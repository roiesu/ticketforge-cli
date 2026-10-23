"""Persisted settings (server URL and username).

The password is deliberately not stored. It is read from the
TICKETFORGE_PASSWORD environment variable or asked for at startup.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

PASSWORD_ENV_VAR = "TICKETFORGE_PASSWORD"


@dataclass
class Config:
    base_url: str
    username: str


def config_path() -> Path:
    base = os.environ.get("TICKETFORGE_CONFIG_DIR") or Path.home() / ".config" / "ticketforge"
    return Path(base) / "config.json"


def load(path: Path | None = None) -> Config | None:
    path = path or config_path()
    try:
        data = json.loads(path.read_text())
        return Config(base_url=data["base_url"], username=data["username"])
    except FileNotFoundError:
        return None
    except (ValueError, KeyError, TypeError):
        # Corrupt or outdated file - treat as "not configured" and run setup again.
        return None


def save(config: Config, path: Path | None = None) -> Path:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(config), indent=2) + "\n")
    return path
