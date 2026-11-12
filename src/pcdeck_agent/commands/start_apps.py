"""Installed Start-menu apps (AppID -> display name), incl. web apps installed from Chrome/Edge.

Used to show a web app's own name (its Start-menu entry) instead of "Chrome" when its media session
plays (web-app AUMIDs look like "Chrome._crx_<id>"). The list comes from PowerShell Get-StartApps
once, in the background (cached).
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time

log = logging.getLogger(__name__)

_lock = threading.Lock()
_apps: dict[str, str] = {}  # AppID -> name
_loaded_at = 0.0
_MIN_REFRESH_S = 60.0


def _load() -> dict[str, str]:
    if sys.platform != "win32":
        return {}
    cmd = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress"
    )
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True, timeout=20, creationflags=0x08000000,
        )
        data = json.loads(r.stdout.decode("utf-8", "replace") or "[]")
    except Exception as e:
        log.info("Start apps unavailable: %s", type(e).__name__)
        return {}
    if isinstance(data, dict):
        data = [data]
    return {str(d.get("AppID")): str(d.get("Name")) for d in data if d.get("AppID") and d.get("Name")}


def apps(refresh: bool = False) -> dict[str, str]:
    global _apps, _loaded_at
    with _lock:
        if (not _loaded_at) or (refresh and time.monotonic() - _loaded_at > _MIN_REFRESH_S):
            _apps = _load()
            _loaded_at = time.monotonic()
            log.info("Start apps: %d entries", len(_apps))
        return _apps


def name_for(aumid: str | None) -> str | None:
    """Display name of an installed app by AUMID (None when unknown). Never blocks on a miss."""
    if not aumid:
        return None
    with _lock:
        return _apps.get(aumid)


def preload_async() -> None:
    threading.Thread(target=apps, name="pcdeck-startapps", daemon=True).start()
