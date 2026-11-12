"""Pick the hardware provider, battery reader and command backend for the running OS.

Windows keeps its original implementations (LibreHardwareMonitor, Windows power status + WMI,
Core Audio + SMTC); Linux and macOS get their own. Every platform module is imported lazily, so the
package imports on any OS and ``sys.platform`` is read at call time (tests monkeypatch it).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

WINDOWS, LINUX, MACOS = "windows", "linux", "macos"


def current() -> str:
    p = sys.platform
    if p == "win32":
        return WINDOWS
    if p == "darwin":
        return MACOS
    if p.startswith("linux"):
        return LINUX
    return p


def hardware_provider() -> Any:
    """A ``HardwareMetricsProvider`` for this OS (not opened yet)."""
    os_name = current()
    if os_name == WINDOWS:
        from .hardware.lhm_provider import LhmProvider

        return LhmProvider()
    if os_name == LINUX:
        from .hardware.linux_provider import LinuxProvider

        return LinuxProvider()
    if os_name == MACOS:
        from .hardware.macos_provider import MacProvider

        return MacProvider()
    raise RuntimeError(f"no hardware provider for {sys.platform}; use --fake")


def battery_reader() -> Any:
    """Object with ``read() -> BatteryMetrics | None``."""
    os_name = current()
    if os_name == LINUX:
        from .hardware.linux_battery import LinuxBatteryReader

        return LinuxBatteryReader()
    if os_name == MACOS:
        from .hardware.macos_battery import MacBatteryReader

        return MacBatteryReader()
    from .hardware.battery import BatteryReader  # Windows (and psutil elsewhere)

    return BatteryReader()


def command_backend(allow_power: Callable[[], bool]) -> Any:
    """The volume / media / lock / power backend, or None on an unsupported OS. May raise."""
    os_name = current()
    if os_name == WINDOWS:
        from .commands.router import WindowsBackend

        return WindowsBackend(allow_power)
    if os_name == LINUX:
        from .commands.linux_media import LinuxBackend

        return LinuxBackend(allow_power)
    if os_name == MACOS:
        from .commands.macos_media import MacBackend

        return MacBackend(allow_power)
    return None
