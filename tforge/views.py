"""Rendering helpers: turn API objects into rich tables/panels."""

from __future__ import annotations

from datetime import datetime

from rich import box
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

STAGE_STYLES = {
    "open": "cyan",
    "in_progress": "yellow",
    "review": "magenta",
    "closed": "green",
}


def stage_text(stage: str) -> Text:
    return Text(stage, style=STAGE_STYLES.get(stage, "white"))


def format_time(value: str | None) -> str:
    if not value:
        return "-"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def truncate(value: str | None, width: int) -> str:
    if not value:
        return ""
    value = " ".join(value.split())
    return value if len(value) <= width else value[: width - 1] + "…"


def tickets_table(tickets: list[dict], title: str) -> Table:
    table = Table(title=title, box=box.SIMPLE_HEAVY, title_justify="left")
    table.add_column("Ref", style="bold", no_wrap=True)
    table.add_column("Title")
    table.add_column("Stage", no_wrap=True)
    table.add_column("Description", style="dim")
    table.add_column("Updated", no_wrap=True, style="dim")
    for t in tickets:
        table.add_row(
            t["ref"],
            truncate(t.get("title"), 40),
            stage_text(t.get("stage", "")),
            truncate(t.get("description"), 40),
            format_time(t.get("updated") or t.get("created")),
        )
    return table


def ticket_panel(ticket: dict, field_labels: dict[str, str] | None = None) -> Panel:
    field_labels = field_labels or {}
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column()

    grid.add_row("Stage", stage_text(ticket.get("stage", "")))
    grid.add_row("Description", ticket.get("description") or Text("(none)", style="dim italic"))
    deps = ticket.get("dependsOn") or []
    grid.add_row("Depends on", ", ".join(deps) if deps else Text("(none)", style="dim italic"))
    for name, value in (ticket.get("customFields") or {}).items():
        grid.add_row(field_labels.get(name, name), str(value))
    owner = (ticket.get("owner") or {}).get("username")
    if owner:
        grid.add_row("Owner", owner)
    grid.add_row("Created", format_time(ticket.get("created")))
    grid.add_row("Updated", format_time(ticket.get("updated")))

    return Panel(grid, title=f"[bold]{ticket['ref']}[/bold]  {ticket.get('title', '')}", title_align="left")


def board_table(tickets: list[dict]) -> Table:
    """Kanban-style overview: one column per stage."""
    columns: dict[str, list[dict]] = {stage: [] for stage in STAGE_STYLES}
    for t in tickets:
        columns.setdefault(t.get("stage", "unknown"), []).append(t)

    table = Table(box=box.ROUNDED, expand=True)
    cells = []
    for stage, items in columns.items():
        table.add_column(f"{stage} ({len(items)})", header_style=f"bold {STAGE_STYLES.get(stage, 'white')}", ratio=1)
        cell = Text()
        for t in items:
            if cell:
                cell.append("\n")
            cell.append(t["ref"], style="bold")
            cell.append(f" {truncate(t.get('title'), 30)}")
        cells.append(cell or Text("-", style="dim"))
    table.add_row(*cells)
    return table


def custom_fields_table(fields: list[dict]) -> Table:
    table = Table(title="Custom fields", box=box.SIMPLE_HEAVY, title_justify="left")
    table.add_column("Name", style="bold")
    table.add_column("Label")
    table.add_column("Type", style="dim")
    for f in fields:
        table.add_row(f["name"], f["label"], f.get("type", ""))
    return table
