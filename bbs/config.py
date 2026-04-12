"""
bbs/config.py — load and validate bbs.yaml configuration.
All other modules import from here; never read yaml directly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class BBSConfig:
    # Core identity
    callsign: str
    ssid: int
    name: str
    sysop: str
    location: str
    max_users: int
    idle_timeout: int

    # Sub-sections as raw dicts (validated at use-site)
    transports: dict[str, Any]
    database: dict[str, Any]
    auth: dict[str, Any]
    plugins: dict[str, Any]
    web: dict[str, Any]
    logging: dict[str, Any]

    @property
    def full_callsign(self) -> str:
        """Return 'CALL-SSID' string, e.g. 'N0CALL-1'."""
        return f"{self.callsign}-{self.ssid}" if self.ssid else self.callsign

    @property
    def db_path(self) -> Path:
        return Path(self.database.get("path", "data/bbs.db"))

    @property
    def connection_log_days(self) -> int:
        return int(self.database.get("connection_log_days", 30))

    @property
    def totp_time_step(self) -> int:
        return int(self.auth.get("totp_time_step", 30))

    @property
    def auth_max_attempts(self) -> int:
        return int(self.auth.get("max_attempts", 3))

    @property
    def auth_lockout_seconds(self) -> int:
        return int(self.auth.get("lockout_seconds", 900))

    @property
    def web_host(self) -> str:
        return str(self.web.get("host", "127.0.0.1"))

    @property
    def web_port(self) -> int:
        return int(self.web.get("port", 8080))

    @property
    def web_secret_key(self) -> str:
        key = str(self.web.get("secret_key", ""))
        if not key or key == "CHANGE_ME_BEFORE_RUNNING":
            raise ValueError(
                "web.secret_key must be set to a unique random string in bbs.yaml"
            )
        return key

    @property
    def sysop_password_hash(self) -> str:
        return str(self.web.get("sysop_password_hash", ""))


def load_config(path: str | Path = "config/bbs.yaml") -> BBSConfig:
    """Load configuration from *path*. Raises FileNotFoundError or ValueError on problems."""
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open() as fh:
        raw: dict[str, Any] = yaml.safe_load(fh) or {}

    bbs = raw.get("bbs", {})
    callsign = bbs.get("callsign", "").upper().strip()
    if not callsign:
        raise ValueError("bbs.callsign must be set in config")
    if len(callsign) > 6:
        raise ValueError(f"bbs.callsign '{callsign}' exceeds 6 characters (AX.25 limit)")

    return BBSConfig(
        callsign=callsign,
        ssid=int(bbs.get("ssid", 0)),
        name=str(bbs.get("name", "Amateur Radio BBS")),
        sysop=str(bbs.get("sysop", callsign)),
        location=str(bbs.get("location", "")),
        max_users=int(bbs.get("max_users", 20)),
        idle_timeout=int(bbs.get("idle_timeout", 300)),
        transports=raw.get("transports", {}),
        database=raw.get("database", {}),
        auth=raw.get("auth", {}),
        plugins=raw.get("plugins", {}),
        web=raw.get("web", {}),
        logging=raw.get("logging", {}),
    )
