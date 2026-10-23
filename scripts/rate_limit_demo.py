"""Show the client's rate-limit handling against the real server.

Sends 55 cheap requests in a row (the limit is 50 per minute) using the account
from `tforge setup`. Password: TICKETFORGE_PASSWORD or a prompt.

    python scripts/rate_limit_demo.py
"""

import os
import time
from getpass import getpass

from tforge import config
from tforge.api import TicketForgeClient

REQUESTS = 55


def main() -> None:
    cfg = config.load()
    if cfg is None:
        raise SystemExit("Run 'tforge setup' first.")
    password = os.environ.get(config.PASSWORD_ENV_VAR) or getpass(f"Password for {cfg.username}: ")

    def on_wait(reason: str, seconds: float) -> None:
        print(f"  -> {reason} - retrying in {seconds:.0f}s...", flush=True)

    client = TicketForgeClient(cfg.base_url, cfg.username, password, on_wait=on_wait)
    print(f"Sending {REQUESTS} requests in a row (server limit is 50 per 60s)", flush=True)
    start = time.monotonic()
    for i in range(1, REQUESTS + 1):
        client.list_custom_fields()
        remaining, limit = client.rate_limit
        if i % 10 == 0 or remaining < 3 or i > 50:
            print(f"request {i:2d} ok   quota {remaining}/{limit}", flush=True)
    print(f"All {REQUESTS} requests succeeded in {time.monotonic() - start:.0f}s")


if __name__ == "__main__":
    main()
