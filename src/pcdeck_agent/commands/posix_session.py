"""Lock / shut down / restart on Linux and macOS (the Windows versions live in router / windows_media).

Same rules as Windows: power actions only exist when the owner enabled them in the PC app, and they
run after POWER_DELAY_S with a visible notice. (Windows' ``shutdown /t 10`` can be aborted with
``shutdown /a``; here quitting the PC app within those 10 s cancels it.)

- Linux lock: ``loginctl lock-session`` (systemd-logind, asks the desktop's locker), else
  ``xdg-screensaver lock``. Power: ``systemctl poweroff|reboot`` (allowed by polkit for the user of
  the active local session), notice via ``notify-send`` when installed.
- macOS lock: ``SACLockScreenImmediate`` (login.framework, what the Lock Screen menu item uses), else
  ``pmset displaysleepnow`` (locks when "require password after sleep" is immediate). Power:
  ``osascript`` "tell application "System Events" to shut down|restart" (Automation permission), notice
  via ``display notification``.
"""

from __future__ import annotations

import ctypes
import logging
import shutil
import subprocess
import threading
from collections.abc import Callable

log = logging.getLogger(__name__)

POWER_DELAY_S = 10
MESSAGES = {
    "shutdown": "PC Control Deck: apagado solicitado desde el dispositivo / shutdown requested from the device",
    "restart": "PC Control Deck: reinicio solicitado desde el dispositivo / restart requested from the device",
}
Runner = Callable[[list[str]], bool]


def run_ok(cmd: list[str]) -> bool:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def lock_linux(run: Runner = run_ok, which: Callable[[str], str | None] = shutil.which) -> bool:
    if which("loginctl") and run(["loginctl", "lock-session"]):
        return True
    return bool(which("xdg-screensaver")) and run(["xdg-screensaver", "lock"])


def _sac_lock() -> bool:
    try:
        login = ctypes.CDLL("/System/Library/PrivateFrameworks/login.framework/Versions/Current/login")
        login.SACLockScreenImmediate.restype = ctypes.c_int
        return login.SACLockScreenImmediate() == 0
    except (OSError, AttributeError):
        return False


def lock_macos(run: Runner = run_ok, sac: Callable[[], bool] = _sac_lock) -> bool:
    return sac() or run(["pmset", "displaysleepnow"])


def power_commands(os_name: str, op: str) -> tuple[list[str], list[str] | None]:
    """(action command, notice command or None) for 'shutdown' / 'restart'."""
    msg = MESSAGES[op]
    if os_name == "linux":
        notice = ["notify-send", "-u", "critical", "-a", "PC Control Deck", msg] if shutil.which("notify-send") else None
        return ["systemctl", "poweroff" if op == "shutdown" else "reboot"], notice
    verb = "shut down" if op == "shutdown" else "restart"
    return (
        ["osascript", "-e", f'tell application "System Events" to {verb}'],
        ["osascript", "-e", f'display notification "{msg}" with title "PC Control Deck"'],
    )


def schedule_power(os_name: str, op: str, run: Runner = run_ok, delay_s: float = POWER_DELAY_S,
                   start: Callable[[threading.Timer], None] = lambda t: t.start()) -> bool:  # fmt: skip
    """Show the notice now and run the action after ``delay_s``. False if the tool is missing."""
    if op not in MESSAGES:
        return False
    cmd, notice = power_commands(os_name, op)
    if shutil.which(cmd[0]) is None:
        log.warning("%s unavailable: %s not found", op, cmd[0])
        return False
    if notice:
        run(notice)

    def fire() -> None:
        if not run(cmd):
            log.warning("%s failed: %s", op, " ".join(cmd))

    timer = threading.Timer(delay_s, fire)
    timer.daemon = True
    start(timer)
    log.info("%s in %d s (requested from a device)", op, delay_s)
    return True
