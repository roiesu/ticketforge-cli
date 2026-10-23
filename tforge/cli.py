"""Interactive terminal UI for TicketForge."""

from __future__ import annotations

import argparse
import os
import re
import sys

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

from . import config, views
from .api import DEFAULT_BASE_URL, STAGES, ApiError, AuthError, TicketForgeClient
from .config import PASSWORD_ENV_VAR, Config

console = Console()

REF_RE = re.compile(r"^[A-Z]+-\d+$")
FIELD_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
BOARD_LIMIT = 100  # 20 requests at 5 per page - well within the 50/min rate limit


class InvalidInput(ValueError):
    pass


# --- input helpers ------------------------------------------------------


def normalize_ref(raw: str) -> str:
    """'tf-12' -> 'TF-12', and a bare number '12' -> 'TF-12'."""
    ref = raw.strip().upper()
    if ref.isdigit():
        ref = f"TF-{ref}"
    if not REF_RE.match(ref):
        raise InvalidInput(f"'{raw.strip()}' is not a ticket ref (expected something like TF-12)")
    return ref


def parse_refs(raw: str | None) -> list[str]:
    """Parse a comma/space separated list of refs, dropping duplicates."""
    refs: list[str] = []
    for token in re.split(r"[,\s]+", raw or ""):
        if token:
            ref = normalize_ref(token)
            if ref not in refs:
                refs.append(ref)
    return refs


def ask_optional(label: str) -> str | None:
    value = Prompt.ask(f"{label} [dim](optional)[/dim]", default="", show_default=False).strip()
    return value or None


def ask_required(label: str) -> str:
    while True:
        value = Prompt.ask(label).strip()
        if value:
            return value
        console.print("[yellow]This field is required.[/yellow]")


def ask_edit(label: str, current: str | None, *, clearable: bool = True) -> str | None:
    """Ask for a new value. Enter keeps the current one; '-' clears it."""
    shown = escape(views.truncate(current, 50)) if current else "none"
    raw = Prompt.ask(f"{label} [dim]({shown})[/dim]", default="", show_default=False).strip()
    if not raw:
        return current
    if clearable and raw == "-":
        return None
    return raw


def _describe(value) -> str:
    if not value:
        return "-"
    if isinstance(value, list):
        return ", ".join(value)
    return views.truncate(str(value), 50)


def ask_ref(prompt: str = "Ticket ref") -> str | None:
    while True:
        raw = Prompt.ask(f"{prompt} [dim](e.g. TF-12, Enter to cancel)[/dim]", default="", show_default=False)
        if not raw.strip():
            return None
        try:
            return normalize_ref(raw)
        except InvalidInput as exc:
            console.print(f"[yellow]{escape(str(exc))}[/yellow]")


# --- setup / login ------------------------------------------------------


def print_error(exc: Exception) -> None:
    console.print(f"[red]Error:[/red] {escape(str(exc))}")


def _notify_wait(reason: str, seconds: float) -> None:
    console.print(f"[yellow]{reason} - retrying in {seconds:.0f}s...[/yellow]")


def make_client(base_url: str, username: str, password: str) -> TicketForgeClient:
    return TicketForgeClient(base_url, username, password, on_wait=_notify_wait)


def run_setup(existing: Config | None = None) -> tuple[TicketForgeClient, Config]:
    console.print(Panel.fit("Connect this CLI to your TicketForge account.", title="Setup"))
    base_url = Prompt.ask("Server URL", default=existing.base_url if existing else DEFAULT_BASE_URL)
    if base_url.startswith("http://"):
        console.print("[yellow]Warning: this URL is not HTTPS - your password will be sent unencrypted.[/yellow]")
    has_account = Confirm.ask("Do you already have a TicketForge account?", default=True)

    while True:
        if existing and has_account:
            username = Prompt.ask("Username", default=existing.username).strip()
        else:
            username = ask_required("Username")
        password = Prompt.ask("Password", password=True)
        client = make_client(base_url, username, password)
        try:
            if not has_account:
                with console.status("Creating account..."):
                    client.register(username, password)
                console.print("[green]Account created.[/green]")
                has_account = True
            with console.status("Checking credentials..."):
                client.verify_credentials()
            break
        except AuthError:
            console.print("[red]Username or password is incorrect.[/red]")
        except ApiError as exc:
            console.print(f"[red]{escape(str(exc))}[/red]")
            if exc.code is None:
                # Not a TicketForge error body - most likely a wrong URL or no network.
                base_url = Prompt.ask("Server URL", default=base_url)

    cfg = Config(base_url=base_url.rstrip("/"), username=username)
    path = config.save(cfg)
    console.print(f"[green]Connected as {escape(username)}.[/green] Settings saved to {path}")
    console.print(f"[dim]Tip: export {PASSWORD_ENV_VAR} to skip the password prompt.[/dim]")
    return client, cfg


def login(cfg: Config) -> TicketForgeClient:
    password = os.environ.get(PASSWORD_ENV_VAR)
    for _ in range(3):
        if password is None:
            password = Prompt.ask(f"Password for [bold]{escape(cfg.username)}[/bold]", password=True)
        client = make_client(cfg.base_url, cfg.username, password)
        try:
            with console.status("Signing in..."):
                client.verify_credentials()
            return client
        except AuthError:
            console.print("[red]Wrong password.[/red]")
            password = None
    raise SystemExit("Could not sign in. Run 'tforge setup' to use a different account.")


# --- main menu ----------------------------------------------------------


class App:
    def __init__(self, client: TicketForgeClient, cfg: Config):
        self.client = client
        self.cfg = cfg
        self._fields: list[dict] | None = None

    def run(self) -> None:
        actions = {
            "1": ("List my tickets", self.list_tickets),
            "2": ("Board view (tickets by stage)", self.show_board),
            "3": ("View a ticket", self.view_ticket),
            "4": ("Create a ticket", self.create_ticket),
            "5": ("Update a ticket", self.update_ticket),
            "6": ("Delete a ticket", self.delete_ticket),
            "7": ("Manage custom fields", self.manage_custom_fields),
            "8": ("Settings (run setup again)", self.settings),
            "q": ("Quit", None),
        }
        while True:
            self.print_header()
            for key, (label, _) in actions.items():
                console.print(f"  [bold cyan]{key}[/bold cyan]  {label}")
            choice = Prompt.ask("\nChoose", choices=list(actions), show_choices=False, case_sensitive=False)
            _, handler = actions[choice]
            if handler is None:
                return
            console.print()
            try:
                handler()
            except AuthError as exc:
                console.print(f"[red]Authentication failed:[/red] {escape(str(exc))}. Try 'Settings'.")
            except (ApiError, InvalidInput) as exc:
                print_error(exc)
            except KeyboardInterrupt:
                console.print("\n[dim]Cancelled.[/dim]")
            console.print()

    def print_header(self) -> None:
        quota = ""
        if self.client.rate_limit:
            remaining, limit = self.client.rate_limit
            quota = f" · API quota {remaining}/{limit}"
        console.rule(f"[bold]TicketForge[/bold] · signed in as {escape(self.cfg.username)}{quota}")

    def custom_fields(self, refresh: bool = False) -> list[dict]:
        if self._fields is None or refresh:
            self._fields = self.client.list_custom_fields()
        return self._fields

    def field_labels(self) -> dict[str, str]:
        return {f["name"]: f["label"] for f in self.custom_fields()}

    # --- tickets ---

    def list_tickets(self) -> None:
        cursors: list[str | None] = [None]  # cursor of every page visited, for "previous"
        page = None
        while True:
            if page is None:
                with console.status("Loading tickets..."):
                    page = self.client.list_tickets(cursors[-1])
            page_no = len(cursors)
            if not page.items and page_no == 1:
                console.print("You have no tickets yet - choose 'Create a ticket' to add one.")
                return

            console.print(views.tickets_table(page.items, f"My tickets · page {page_no}"))
            options = []
            if page.has_more:
                options.append("[bold]n[/bold] next")
            if page_no > 1:
                options.append("[bold]p[/bold] previous")
            options += ["[bold]v <ref>[/bold] view", "Enter to go back"]
            cmd, _, arg = Prompt.ask(" · ".join(options), default="", show_default=False).strip().partition(" ")

            cmd = cmd.lower()
            if not cmd:
                return
            if cmd == "n" and page.has_more:
                cursors.append(page.next_cursor)
                page = None
            elif cmd == "p" and page_no > 1:
                cursors.pop()
                page = None
            elif cmd == "v":
                try:
                    self.view_ticket(normalize_ref(arg) if arg.strip() else None)
                except (ApiError, InvalidInput) as exc:
                    print_error(exc)
            else:
                console.print("[yellow]Unknown option.[/yellow]")

    def show_board(self) -> None:
        with console.status("Loading all tickets..."):
            tickets = list(self.client.iter_tickets(max_items=BOARD_LIMIT + 1))
        if not tickets:
            console.print("You have no tickets yet - choose 'Create a ticket' to add one.")
            return
        console.print(views.board_table(tickets[:BOARD_LIMIT]))
        if len(tickets) > BOARD_LIMIT:
            console.print(f"[dim]Showing your {BOARD_LIMIT} most recent tickets.[/dim]")

    def view_ticket(self, ref: str | None = None) -> None:
        ref = ref or ask_ref()
        if not ref:
            return
        with console.status(f"Loading {ref}..."):
            ticket = self.client.get_ticket(ref)
            labels = self.field_labels()
        console.print(views.ticket_panel(ticket, labels))

    def create_ticket(self) -> None:
        title = ask_required("Title")
        description = ask_optional("Description")
        depends_on = parse_refs(ask_optional("Depends on, comma-separated refs"))

        values = {}
        fields = self.custom_fields()
        if fields:
            console.print("[dim]Custom fields - press Enter to skip:[/dim]")
            for f in fields:
                value = Prompt.ask(f"  {escape(f['label'])}", default="", show_default=False).strip()
                if value:
                    values[f["name"]] = value

        with console.status("Creating ticket..."):
            ticket = self.client.create_ticket(title, description, depends_on, values)
        console.print(f"[green]Created {ticket['ref']}[/green] - {escape(ticket['title'])}")

    def update_ticket(self) -> None:
        ref = ask_ref("Ticket to update")
        if not ref:
            return
        with console.status(f"Loading {ref}..."):
            ticket = self.client.get_ticket(ref)
            fields = self.custom_fields()
        console.print(views.ticket_panel(ticket, self.field_labels()))
        console.print("[dim]Press Enter to keep a value, or '-' to clear it.[/dim]")

        changes: dict = {}
        title = ask_edit("Title", ticket["title"], clearable=False)
        if title != ticket["title"]:
            changes["title"] = title

        description = ask_edit("Description", ticket.get("description"))
        if description != ticket.get("description"):
            changes["description"] = description

        stage = Prompt.ask("Stage", choices=list(STAGES), default=ticket["stage"], case_sensitive=False)
        if stage != ticket["stage"]:
            changes["stage"] = stage

        current_deps = ticket.get("dependsOn") or []
        deps = parse_refs(ask_edit("Depends on", ", ".join(current_deps) or None))
        if deps != current_deps:
            changes["dependsOn"] = deps

        current_values = ticket.get("customFields") or {}
        new_values = dict(current_values)
        for f in fields:
            value = ask_edit(f"  {escape(f['label'])}", current_values.get(f["name"]))
            if value is None:
                new_values.pop(f["name"], None)
            else:
                new_values[f["name"]] = value
        if new_values != current_values:
            changes["customFields"] = new_values

        if not changes:
            console.print("Nothing changed.")
            return

        console.print("\n[bold]Changes:[/bold]")
        for label, before, after in self._diff(ticket, changes):
            console.print(f"  {escape(label)}: [red]{escape(before)}[/red] → [green]{escape(after)}[/green]")
        if not Confirm.ask("Save these changes?", default=True):
            console.print("[dim]Discarded.[/dim]")
            return

        with console.status("Saving..."):
            updated = self.client.update_ticket(ref, ticket, **changes)
        console.print(f"[green]Updated {ref}.[/green]")
        console.print(views.ticket_panel(updated, self.field_labels()))

    def _diff(self, ticket: dict, changes: dict) -> list[tuple[str, str, str]]:
        """(label, before, after) for every changed value, custom fields one by one."""
        names = {"title": "Title", "description": "Description", "stage": "Stage", "dependsOn": "Depends on"}
        rows = []
        for key, new_value in changes.items():
            if key != "customFields":
                rows.append((names[key], _describe(ticket.get(key)), _describe(new_value)))
                continue
            labels = self.field_labels()
            old_values = ticket.get("customFields") or {}
            for name in sorted(old_values.keys() | new_value.keys()):
                if old_values.get(name) != new_value.get(name):
                    rows.append(
                        (labels.get(name, name), _describe(old_values.get(name)), _describe(new_value.get(name)))
                    )
        return rows

    def delete_ticket(self) -> None:
        ref = ask_ref("Ticket to delete")
        if not ref:
            return
        with console.status(f"Loading {ref}..."):
            ticket = self.client.get_ticket(ref)
        if not Confirm.ask(f"Delete {ref} '{escape(ticket['title'])}'? This cannot be undone", default=False):
            return
        with console.status("Deleting..."):
            self.client.delete_ticket(ref)
        console.print(f"[green]Deleted {ref}.[/green]")

    # --- custom fields ---

    def manage_custom_fields(self) -> None:
        while True:
            with console.status("Loading custom fields..."):
                fields = self.custom_fields(refresh=True)
            if fields:
                console.print(views.custom_fields_table(fields))
            else:
                console.print("No custom fields defined yet.")

            prompt = "[bold]a[/bold] add · [bold]r[/bold] rename · [bold]d[/bold] delete · Enter to go back"
            choice = Prompt.ask(prompt, default="", show_default=False).strip().lower()
            if not choice:
                return
            if choice == "a":
                self._add_custom_field()
            elif choice in ("r", "d") and not fields:
                console.print("[yellow]There are no custom fields yet.[/yellow]")
            elif choice in ("r", "d"):
                field = self._pick_field(fields)
                if choice == "r":
                    label = ask_required(f"New label for '{field['name']}'")
                    self.client.rename_custom_field(field["id"], label)
                    console.print("[green]Renamed.[/green]")
                elif Confirm.ask(f"Delete custom field '{field['name']}'?", default=False):
                    self.client.delete_custom_field(field["id"])
                    console.print("[green]Deleted.[/green]")
            else:
                console.print("[yellow]Unknown option.[/yellow]")

    def _add_custom_field(self) -> None:
        while True:
            name = ask_required("Field name (letters, digits, underscores)")
            if FIELD_NAME_RE.match(name):
                break
            console.print("[yellow]Only letters, digits and underscores are allowed.[/yellow]")
        label = ask_required("Display label")
        self.client.create_custom_field(name, label)
        console.print(f"[green]Added field '{name}'.[/green]")

    def _pick_field(self, fields: list[dict]) -> dict:
        by_name = {f["name"]: f for f in fields}
        name = Prompt.ask("Field name", choices=list(by_name))
        return by_name[name]

    # --- settings ---

    def settings(self) -> None:
        self.client, self.cfg = run_setup(self.cfg)
        self._fields = None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tforge", description="Manage TicketForge tickets from the terminal.")
    parser.add_argument("command", nargs="?", choices=["setup"], help="run the setup wizard (connect / switch account)")
    args = parser.parse_args(argv)

    try:
        cfg = config.load()
        if args.command == "setup" or cfg is None:
            client, cfg = run_setup(cfg)
        else:
            client = login(cfg)
        App(client, cfg).run()
    except (KeyboardInterrupt, EOFError):
        console.print()
    console.print("Bye!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
