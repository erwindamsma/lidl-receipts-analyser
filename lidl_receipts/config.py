"""On-disk configuration: credentials and per-account settings."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

# The Lidl Plus API rejects implausible client versions, so this needs to track
# a real app release now and then. Bump it via `config.json` if calls start
# failing with 400/426 and the body mentions the app version.
DEFAULT_APP_VERSION = "16.45.5"

CONFIG_DIR = Path(
    os.environ.get("LIDL_RECEIPTS_HOME")
    or Path.home() / ".config" / "lidl-receipts"
)
CONFIG_PATH = CONFIG_DIR / "config.json"


@dataclass
class Config:
    country: str = "NL"
    language: str = "nl"
    refresh_token: str = ""
    app_version: str = DEFAULT_APP_VERSION
    data_dir: str = field(
        default_factory=lambda: str(Path.cwd() / "data")
    )

    @classmethod
    def load(cls) -> "Config":
        if not CONFIG_PATH.exists():
            return cls()
        raw = json.loads(CONFIG_PATH.read_text())
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        # Write via a private temp file: the refresh token is a bearer
        # credential and must never exist world-readable, not even briefly.
        tmp = CONFIG_PATH.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(asdict(self), handle, indent=2)
            handle.write("\n")
        tmp.replace(CONFIG_PATH)

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / "receipts.db"

    @property
    def raw_dir(self) -> Path:
        return Path(self.data_dir) / "raw"
