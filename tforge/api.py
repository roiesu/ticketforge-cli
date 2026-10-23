"""Client for the TicketForge REST API.

TicketForge has no public API docs. Endpoints and behaviour were worked out
from the web app's network traffic - see docs/API_NOTES.md.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from urllib.parse import quote

import requests

DEFAULT_BASE_URL = "https://integrations-assignment-ticketforge.vercel.app"
STAGES = ("open", "in_progress", "review", "closed")

# Only these are retried after a timeout/5xx. Retrying a POST could create a
# duplicate ticket if the first request actually reached the server.
IDEMPOTENT_METHODS = {"GET", "PUT", "DELETE"}


class ApiError(Exception):
    """An error returned by TicketForge (or a failure to reach it)."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class AuthError(ApiError):
    pass


@dataclass
class Page:
    items: list[dict]
    next_cursor: str | None

    @property
    def has_more(self) -> bool:
        return self.next_cursor is not None


class TicketForgeClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        username: str | None = None,
        password: str | None = None,
        *,
        timeout: float = 15,
        max_retries: int = 3,
        on_wait: Callable[[str, float], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.api_url = base_url.rstrip("/") + "/api/tforge"
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "ticketforge-cli"
        if username is not None:
            self.session.auth = (username, password or "")
        self.timeout = timeout
        self.max_retries = max_retries
        self.on_wait = on_wait
        self._sleep = sleep
        self.rate_limit: tuple[int, int] | None = None  # (remaining, limit)

    # --- auth -----------------------------------------------------------

    def register(self, username: str, password: str) -> dict:
        body = self._request("POST", "/user/register", json={"username": username, "password": password})
        return body["user"]

    def verify_credentials(self) -> None:
        """Raises AuthError if the configured username/password are rejected."""
        self._request("GET", "/workitems/mine", params={"limit": 1})

    # --- tickets --------------------------------------------------------

    def list_tickets(self, cursor: str | None = None) -> Page:
        # The server caps page size at 5 and ignores larger limits.
        params = {"cursor": cursor} if cursor else None
        body = self._request("GET", "/workitems/mine", params=params)
        pagination = body.get("pagination") or {}
        next_cursor = pagination.get("nextCursor") if pagination.get("hasMore") else None
        return Page(items=body.get("workitems", []), next_cursor=next_cursor)

    def iter_tickets(self, max_items: int | None = None) -> Iterator[dict]:
        """Yield tickets across all pages (newest first), stopping after max_items."""
        cursor = None
        count = 0
        while True:
            page = self.list_tickets(cursor)
            for ticket in page.items:
                if max_items is not None and count >= max_items:
                    return
                yield ticket
                count += 1
            if not page.has_more:
                return
            cursor = page.next_cursor

    def get_ticket(self, ref: str) -> dict:
        # view=deep adds the owner; customFields/dependsOn are only present when set.
        return self._request("GET", f"/workitem/{quote(ref)}", params={"view": "deep"})["workitem"]

    def create_ticket(
        self,
        title: str,
        description: str | None = None,
        depends_on: list[str] | None = None,
        custom_fields: dict[str, str] | None = None,
    ) -> dict:
        payload: dict = {"title": title, "description": description, "dependsOn": depends_on or None}
        if custom_fields:
            payload["customFields"] = custom_fields
        return self._request("POST", "/workitem/publish", json=payload)["workitem"]

    def update_ticket(self, ref: str, current: dict, **changes) -> dict:
        payload = build_update_payload(current, **changes)
        return self._request("PUT", f"/workitem/{quote(ref)}", json=payload)["workitem"]

    def delete_ticket(self, ref: str) -> None:
        self._request("DELETE", f"/workitem/{quote(ref)}")

    # --- custom fields --------------------------------------------------

    def list_custom_fields(self) -> list[dict]:
        return self._request("GET", "/custom-fields").get("customFields", [])

    def create_custom_field(self, name: str, label: str) -> dict:
        return self._request("POST", "/custom-fields", json={"name": name, "label": label})["customField"]

    def rename_custom_field(self, field_id: str, label: str) -> dict:
        return self._request("PUT", f"/custom-fields/{quote(field_id)}", json={"label": label})["customField"]

    def delete_custom_field(self, field_id: str) -> None:
        self._request("DELETE", f"/custom-fields/{quote(field_id)}")

    # --- transport ------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = self.api_url + path
        idempotent = method in IDEMPOTENT_METHODS
        for attempt in range(self.max_retries + 1):
            last_attempt = attempt == self.max_retries
            delay = 2**attempt
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except requests.RequestException as exc:
                if last_attempt or not idempotent:
                    raise ApiError(f"Could not reach TicketForge ({exc.__class__.__name__})") from exc
                self._wait("Connection problem", delay)
                continue

            self._track_rate_limit(resp)
            # A 429 means the request was rejected, not processed, so even a POST is safe to repeat.
            if resp.status_code == 429 and not last_attempt:
                self._wait("Rate limit reached", _retry_after(resp, default=delay))
                continue
            if resp.status_code >= 500 and idempotent and not last_attempt:
                self._wait(f"Server error ({resp.status_code})", delay)
                continue
            return _parse(resp)

    def _wait(self, reason: str, seconds: float) -> None:
        if self.on_wait:
            self.on_wait(reason, seconds)
        self._sleep(seconds)

    def _track_rate_limit(self, resp: requests.Response) -> None:
        try:
            self.rate_limit = (
                int(resp.headers["X-RateLimit-Remaining"]),
                int(resp.headers["X-RateLimit-Limit"]),
            )
        except (KeyError, ValueError):
            pass


def build_update_payload(current: dict, **changes) -> dict:
    """Build a full PUT body for a ticket.

    PUT /workitem/{ref} is a full replace: a missing title/stage is rejected and
    a missing description/customFields is cleared. So we always start from the
    ticket's current state and apply the requested changes on top.
    """
    allowed = {"title", "description", "stage", "dependsOn", "customFields"}
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"Unknown ticket field(s): {', '.join(sorted(unknown))}")

    payload = {
        "title": current["title"],
        "description": current.get("description"),
        "stage": current["stage"],
        "dependsOn": current.get("dependsOn") or None,
        "customFields": dict(current.get("customFields") or {}),
    }
    payload.update(changes)
    return payload


def _retry_after(resp: requests.Response, default: float) -> float:
    try:
        return max(1.0, float(resp.headers["Retry-After"]))
    except (KeyError, ValueError):
        return default


def _parse(resp: requests.Response) -> dict:
    try:
        body = resp.json()
    except ValueError:
        body = None

    if resp.ok and isinstance(body, dict):
        return body

    if not isinstance(body, dict):
        raise ApiError(f"Unexpected response from TicketForge (HTTP {resp.status_code})", status=resp.status_code)

    message = body.get("message") or body.get("err") or f"HTTP {resp.status_code}"
    error_cls = AuthError if resp.status_code == 401 else ApiError
    raise error_cls(message, status=resp.status_code, code=body.get("err"))
