"""Laptop battery: charge %, AC adapter, charging, time left and remaining energy.

Windows: GetSystemPowerStatus (always available, cheap) for %, AC, charging flag and time left;
root\\WMI BatteryStatus / BatteryFullChargedCapacity (via System.Management, needs pythonnet
loaded by LHM) for remaining/full energy and charge/discharge rate, refreshed every few seconds.
Elsewhere: psutil.sensors_battery(). A PC without a battery reports None.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

WMI_REFRESH_S = 10.0


@dataclass
class BatteryMetrics:
    pct: int | None = None
    plugged: bool | None = None  # AC adapter connected
    charging: bool | None = None
    secs_left: int | None = None  # on battery: estimated time to empty
    remaining_wh: float | None = None
    full_wh: float | None = None
    rate_w: float | None = None  # > 0 charging, < 0 discharging


class _SystemPowerStatus(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", ctypes.c_uint32),
        ("BatteryFullLifeTime", ctypes.c_uint32),
    ]


_NO_BATTERY = 128
_UNKNOWN = 255
_CHARGING = 8
_UNKNOWN_TIME = 0xFFFFFFFF


class BatteryReader:
    def __init__(self) -> None:
        self._wmi_next = 0.0
        self._wmi: tuple[float | None, float | None, float | None] = (None, None, None)
        self._wmi_ok = sys.platform == "win32"

    def read(self) -> BatteryMetrics | None:
        try:
            b = self._read_windows() if sys.platform == "win32" else self._read_psutil()
        except Exception:
            log.debug("battery read failed", exc_info=True)
            return None
        if b is not None and self._wmi_ok:
            now = time.monotonic()
            if now >= self._wmi_next:
                self._wmi_next = now + WMI_REFRESH_S
                self._wmi = self._read_wmi()
            b.remaining_wh, b.full_wh, b.rate_w = self._wmi
        return b

    @staticmethod
    def _read_windows() -> BatteryMetrics | None:
        st = _SystemPowerStatus()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):
            return None
        if st.BatteryFlag in (_NO_BATTERY, _UNKNOWN) or st.BatteryLifePercent == _UNKNOWN:
            return None
        plugged = {0: False, 1: True}.get(st.ACLineStatus)
        return BatteryMetrics(
            pct=min(100, int(st.BatteryLifePercent)),
            plugged=plugged,
            charging=bool(st.BatteryFlag & _CHARGING),
            secs_left=None if plugged or st.BatteryLifeTime == _UNKNOWN_TIME else int(st.BatteryLifeTime),
        )

    @staticmethod
    def _read_psutil() -> BatteryMetrics | None:
        import psutil

        b = psutil.sensors_battery()
        if b is None:
            return None
        secs = b.secsleft if isinstance(b.secsleft, int) and b.secsleft >= 0 else None
        return BatteryMetrics(pct=round(b.percent), plugged=b.power_plugged, charging=None, secs_left=secs)

    def _read_wmi(self) -> tuple[float | None, float | None, float | None]:
        try:
            import clr  # type: ignore[import-not-found]

            clr.AddReference("System.Management")
            from System.Management import ManagementObjectSearcher  # type: ignore[import-not-found]

            remaining = rate = full = None
            q = "SELECT RemainingCapacity, ChargeRate, DischargeRate, Charging, Discharging FROM BatteryStatus"
            for o in ManagementObjectSearcher("root\\WMI", q).Get():
                remaining = _mwh(o["RemainingCapacity"])
                if o["Charging"] and _int(o["ChargeRate"]):
                    rate = _int(o["ChargeRate"]) / 1000.0
                elif o["Discharging"] and _int(o["DischargeRate"]):
                    rate = -_int(o["DischargeRate"]) / 1000.0
                else:
                    rate = 0.0
                break
            for o in ManagementObjectSearcher("root\\WMI", "SELECT FullChargedCapacity FROM BatteryFullChargedCapacity").Get():
                full = _mwh(o["FullChargedCapacity"])
                break
            return remaining, full, rate
        except Exception as e:
            log.info("battery WMI not usable (%s): energy/rate omitted", type(e).__name__)
            self._wmi_ok = False
            return None, None, None


def _int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _mwh(v: Any) -> float | None:
    n = _int(v)
    return round(n / 1000.0, 1) if 0 < n < 0x7FFFFFFF else None
