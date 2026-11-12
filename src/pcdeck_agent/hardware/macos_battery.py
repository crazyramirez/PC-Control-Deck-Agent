"""macOS battery: ``ioreg -rn AppleSmartBattery`` (charge, mAh × V -> Wh, signed amperage -> W, time
left), with psutil as a fallback for the percentage. Refreshed at most every few seconds (ioreg is a
subprocess). A Mac without a battery reports None."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from .battery import BatteryMetrics
from .macos_parsers import battery_from_ioreg, parse_ioreg_props

log = logging.getLogger(__name__)

REFRESH_S = 5.0


def _run_ioreg() -> str | None:
    from .macos_provider import run_cmd

    return run_cmd(["ioreg", "-rn", "AppleSmartBattery"])


def _psutil_battery() -> BatteryMetrics | None:
    import psutil

    b = psutil.sensors_battery()
    if b is None:
        return None
    secs = b.secsleft if isinstance(b.secsleft, int) and b.secsleft >= 0 and not b.power_plugged else None
    return BatteryMetrics(pct=round(b.percent), plugged=b.power_plugged, charging=None, secs_left=secs)


class MacBatteryReader:
    def __init__(
        self,
        ioreg: Callable[[], str | None] = _run_ioreg,
        fallback: Callable[[], BatteryMetrics | None] = _psutil_battery,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ioreg = ioreg
        self._fallback = fallback
        self._clock = clock
        self._next = 0.0
        self._last: BatteryMetrics | None = None

    def read(self) -> BatteryMetrics | None:
        now = self._clock()
        if now < self._next:
            return self._last
        self._next = now + REFRESH_S
        try:
            text = self._ioreg()
            b = battery_from_ioreg(parse_ioreg_props(text)) if text else None
            self._last = b if b is not None else self._fallback()
        except Exception:
            log.debug("battery read failed", exc_info=True)
            self._last = None
        return self._last
