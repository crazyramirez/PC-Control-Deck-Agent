"""Agent configuration: versioned JSON in the user's local app-data dir, safe defaults.

No secrets are stored here (pairing credentials live in security/credentials.py: DPAPI on Windows,
an owner-only file on Linux/macOS).
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)
CONFIG_SCHEMA = 1


def app_dir() -> Path:
    """Per-user data dir: config.json and the paired-device store.

    Windows ``%LOCALAPPDATA%\\PcControlDeck``; macOS ``~/Library/Application Support/PC Control Deck``
    (the app's name before 0.10: kept, it holds the paired decks and settings);
    Linux ``$XDG_CONFIG_HOME/pcdeck`` (``~/.config/pcdeck``)."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "PcControlDeck"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "PC Control Deck"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "pcdeck"


def log_dir() -> Path:
    """Windows ``<app_dir>\\logs``; macOS ``~/Library/Logs/PC Control Deck``; Linux ``$XDG_STATE_HOME/pcdeck/logs``."""
    if sys.platform == "win32":
        return app_dir() / "logs"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "PC Control Deck"
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "pcdeck" / "logs"


@dataclass
class AgentConfig:
    schema: int = CONFIG_SCHEMA
    # "auto" = primary LAN IPv4 + loopback. Never 0.0.0.0 by default (spec §7: bind to the LAN only).
    bind: list[str] = field(default_factory=lambda: ["auto"])
    port: int = 8765
    interval_s: float = 1.0  # 1–2 Hz per spec
    device_id: str = ""  # empty -> short host name
    cpu_temp_sensor_id: str | None = None
    gpu_hardware_id: str | None = None
    max_clients: int = 4
    log_level: str = "INFO"
    agent_id: str = ""  # random, stable identity advertised over mDNS (not a secret)
    mdns: bool = True
    allow_power: bool = False  # shutdown/restart from the device: opt-in in the PC app (spec §4)
    auto_update: bool = True  # install new app releases from GitHub by itself (updater.py)
    last_version: str = ""  # app version of the previous start: "updated to X" notice
    update_attempt_version: str = ""  # last self-update tried, and when: no retry loop if it failed
    update_attempt_at: float = 0.0

    def normalized(self) -> "AgentConfig":
        if not self.agent_id:
            import secrets

            self.agent_id = secrets.token_hex(6)
        self.interval_s = min(max(float(self.interval_s), 0.5), 1.0)
        if not (1024 <= int(self.port) <= 65535):
            self.port = 8765
        self.max_clients = min(max(int(self.max_clients), 1), 8)
        if not self.device_id:
            self.device_id = socket.gethostname().split(".")[0][:23]
        return self


def load_config(path: Path | None = None) -> tuple[AgentConfig, Path]:
    p = path or app_dir() / "config.json"
    cfg = AgentConfig()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            known = {k: v for k, v in data.items() if k in AgentConfig.__dataclass_fields__}
            cfg = AgentConfig(**known)
        except (ValueError, TypeError) as e:
            log.warning("config %s unreadable (%s); using defaults", p, e)
    cfg = cfg.normalized()
    save_config(cfg, p)  # persists generated fields (agent_id) and new defaults
    return cfg, p


def save_config(cfg: AgentConfig, path: Path | None = None) -> None:
    p = path or app_dir() / "config.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(cfg), indent=2), encoding="utf-8")
    tmp.replace(p)


def primary_lan_ipv4() -> str | None:
    """IPv4 of the interface holding the default route. UDP connect sends no packet."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.254.254.254", 9))
        ip = s.getsockname()[0]
        return None if ip.startswith("0.") else ip
    except OSError:
        return None
    finally:
        s.close()


def resolve_bind(bind: list[str]) -> list[str]:
    out: list[str] = []
    for b in bind:
        if b == "auto":
            ip = primary_lan_ipv4()
            if ip:
                out.append(ip)
            out.append("127.0.0.1")
        else:
            out.append(b)
    return list(dict.fromkeys(out))  # dedupe, keep order
