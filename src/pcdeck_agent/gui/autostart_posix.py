"""Start-at-login switch for Linux and macOS (no Qt, unit-tested on every OS).

- Linux: an XDG autostart entry ``$XDG_CONFIG_HOME/autostart/pcdeck.desktop`` (GNOME, KDE, Xfce...).
- macOS: a LaunchAgent ``~/Library/LaunchAgents/com.pcdeck.agent.plist`` (RunAtLoad, Aqua sessions),
  taken into account at the next login.
Both start the app with ``--minimized`` (tray / menu bar only), not elevated.
"""

from __future__ import annotations

import os
import plistlib
import sys
from pathlib import Path

DESKTOP_NAME = "pcdeck.desktop"
LAUNCH_AGENT_LABEL = "com.pcdeck.agent"


def launch_command() -> list[str]:
    if getattr(sys, "frozen", False):
        exe = os.environ.get("APPIMAGE") or sys.executable  # an AppImage must be relaunched through itself
        return [exe, "--minimized"]
    return [sys.executable, "-m", "pcdeck_agent.gui.app", "--minimized"]


def _home(home: Path | None) -> Path:
    return home if home is not None else Path.home()


def linux_path(home: Path | None = None) -> Path:
    cfg = os.environ.get("XDG_CONFIG_HOME") if home is None else None
    return Path(cfg or _home(home) / ".config") / "autostart" / DESKTOP_NAME


def macos_path(home: Path | None = None) -> Path:
    return _home(home) / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def menu_entry_path(home: Path | None = None) -> Path:
    """The applications-menu entry install.sh writes (Linux)."""
    data = os.environ.get("XDG_DATA_HOME") if home is None else None
    return Path(data or _home(home) / ".local" / "share") / "applications" / DESKTOP_NAME


COMMENT = "media control for your DECKY touch deck"  # tail of the Comment= line


def migrate_names(home: Path | None = None) -> list[Path]:
    """Linux: the app was "PC Control Deck" before 0.10 and "Control Deck" until 1.0. A self-update
    replaces the app folder only, so rename it in the menu and autostart entries it left behind.
    Returns the files changed."""
    changed = []
    for p in (menu_entry_path(home), linux_path(home)):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        new = (
            text.replace("Name=PC Control Deck\n", "Name=DECKY Agent\n")
            .replace("Name=Control Deck\n", "Name=DECKY Agent\n")
            .replace("media control for the PC Control Deck\n", COMMENT + "\n")
            .replace("media control for your PC Control Deck touch deck\n", COMMENT + "\n")
        )
        if new != text:
            p.write_text(new, encoding="utf-8")
            changed.append(p)
    return changed


def _exec_quote(arg: str) -> str:
    """Quote one Exec= argument per the Desktop Entry spec."""
    if arg and not any(c in arg for c in ' \t\n"\'\\><~|&;$*?#()`'):
        return arg
    return '"' + "".join("\\" + c if c in '"`$\\' else c for c in arg) + '"'


def desktop_entry(cmd: list[str]) -> str:
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Name=DECKY Agent",
        "Comment=Hardware telemetry and media control for your DECKY touch deck",
        "Exec=" + " ".join(_exec_quote(a).replace("%", "%%") for a in cmd),
        "Icon=pcdeck",
        "Terminal=false",
        "X-GNOME-Autostart-enabled=true",
        "",
    ])  # fmt: skip


def launch_agent(cmd: list[str]) -> bytes:
    return plistlib.dumps({
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": cmd,
        "RunAtLoad": True,
        "ProcessType": "Interactive",
        "LimitLoadToSessionType": "Aqua",
    })  # fmt: skip


def _platform(platform: str | None) -> str:
    return platform or ("macos" if sys.platform == "darwin" else "linux")


def is_enabled(platform: str | None = None, home: Path | None = None) -> bool:
    if _platform(platform) == "macos":
        return macos_path(home).exists()
    p = linux_path(home)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return False
    lines = {ln.strip().lower() for ln in text.splitlines()}
    return "hidden=true" not in lines and "x-gnome-autostart-enabled=false" not in lines


def set_enabled(on: bool, platform: str | None = None, home: Path | None = None, cmd: list[str] | None = None) -> tuple[bool, str]:
    """Returns (ok, message) like the Windows implementation."""
    macos = _platform(platform) == "macos"
    path = macos_path(home) if macos else linux_path(home)
    try:
        if not on:
            path.unlink(missing_ok=True)
            return True, ""
        path.parent.mkdir(parents=True, exist_ok=True)
        command = cmd or launch_command()
        if macos:
            path.write_bytes(launch_agent(command))
        else:
            path.write_text(desktop_entry(command), encoding="utf-8")
        return True, ""
    except OSError as e:
        return False, str(e)
