"""Start-with-Windows switch.

Installed build: the installer registers two scheduled tasks for the current user:
  "PC Control Deck"          on demand, highest privileges (Start-menu shortcut runs it, no UAC prompt)
  "PC Control Deck (inicio)" at logon, highest privileges  <- this is what the switch enables/disables
Elevation lets the agent read CPU temperatures (Lenovo EC / LibreHardwareMonitor).

Development run (tasks absent): falls back to the per-user Run key (not elevated).
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

TASK_RUN = "PC Control Deck"
TASK_LOGON = "PC Control Deck (inicio)"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "PCControlDeck"
_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks", *args], capture_output=True, text=True, creationflags=_NO_WINDOW, timeout=15)


def task_exists(name: str = TASK_LOGON) -> bool:
    if sys.platform != "win32":
        return False
    return _schtasks("/Query", "/TN", name).returncode == 0


def _task_enabled(name: str) -> bool:
    """Reads <Settings><Enabled> from the task XML (absent element = enabled)."""
    import xml.etree.ElementTree as ET

    r = subprocess.run(["schtasks", "/Query", "/TN", name, "/XML"], capture_output=True, creationflags=_NO_WINDOW, timeout=15)
    if r.returncode != 0:
        return False
    raw = r.stdout
    text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8", "replace")
    try:
        root = ET.fromstring(text.strip())
    except ET.ParseError:
        return True
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    el = root.find("t:Settings/t:Enabled", ns)
    return el is None or (el.text or "").strip().lower() != "false"


def _exe_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return f'"{Path(sys.executable).with_name("pythonw.exe")}" -m pcdeck_agent.gui.app'


def is_enabled() -> bool:
    if sys.platform != "win32":
        return False
    if task_exists():
        return _task_enabled(TASK_LOGON)
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_VALUE)
            return True
    except OSError:
        return False


def set_enabled(on: bool) -> tuple[bool, str]:
    """Returns (ok, message)."""
    if sys.platform != "win32":
        return False, "only supported on Windows"
    if task_exists():
        r = _schtasks("/Change", "/TN", TASK_LOGON, "/ENABLE" if on else "/DISABLE")
        if r.returncode != 0:
            msg = (r.stderr or r.stdout).strip()
            log.warning("schtasks change failed: %s", msg)
            return False, msg or "schtasks failed (administrator rights needed)"
        return True, ""
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, _exe_command())
        else:
            try:
                winreg.DeleteValue(k, RUN_VALUE)
            except FileNotFoundError:
                pass
    return True, ""
