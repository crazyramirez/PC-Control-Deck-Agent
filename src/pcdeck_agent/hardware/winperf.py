"""CPU clock and package power from Windows performance counters (no driver, no admin).

Without the PawnIO driver LibreHardwareMonitor 0.9.6 cannot read the CPU MSRs, so core clocks and
package power are missing. Windows exposes both through PDH:

- effective clock = "Processor Frequency" x "% Processor Performance" / 100 (what Task Manager shows);
- package power   = "Energy Meter(RAPL_Package0_PKG)\\Power" in mW (Intel RAPL through the
  Energy Metering Interface; not every platform has it).

English counter paths are used (PdhAddEnglishCounterW), so localized Windows works too. Rate counters
need two collections: the first read after open() returns nothing.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes

log = logging.getLogger(__name__)

_FREQ = r"\Processor Information(_Total)\Processor Frequency"
_PERF = r"\Processor Information(_Total)\% Processor Performance"
_PKG = r"\Energy Meter(RAPL_Package0_PKG)\Power"
_PDH_FMT_DOUBLE = 0x00000200


class _FmtValue(ctypes.Structure):
    _fields_ = [("CStatus", wintypes.DWORD), ("pad", wintypes.DWORD), ("doubleValue", ctypes.c_double)]


class WinPerfCpu:
    def __init__(self) -> None:
        self._pdh = None
        self._q = wintypes.HANDLE()
        self._h: dict[str, wintypes.HANDLE] = {}

    def open(self) -> None:
        if sys.platform != "win32":
            return
        try:
            pdh = ctypes.WinDLL("pdh")
            if pdh.PdhOpenQueryW(None, None, ctypes.byref(self._q)) != 0:
                return
            for path in (_FREQ, _PERF, _PKG):
                h = wintypes.HANDLE()
                if pdh.PdhAddEnglishCounterW(self._q, path, None, ctypes.byref(h)) == 0:
                    self._h[path] = h
            self._pdh = pdh
            pdh.PdhCollectQueryData(self._q)  # prime rate counters
            log.info("Windows CPU counters: %s", ", ".join(p.rsplit("\\", 1)[-1] for p in self._h) or "none")
        except Exception as e:
            log.info("Windows CPU counters unavailable: %s", type(e).__name__)
            self._pdh = None

    def _value(self, path: str) -> float | None:
        h = self._h.get(path)
        if h is None or self._pdh is None:
            return None
        v = _FmtValue()
        if self._pdh.PdhGetFormattedCounterValue(h, _PDH_FMT_DOUBLE, None, ctypes.byref(v)) != 0:
            return None
        return float(v.doubleValue)

    def read(self) -> tuple[float | None, float | None]:
        """(effective clock MHz, package power W); None when unknown. Call about once per second."""
        if self._pdh is None:
            return None, None
        try:
            self._pdh.PdhCollectQueryData(self._q)
        except Exception:
            return None, None
        freq, perf, pkg_mw = self._value(_FREQ), self._value(_PERF), self._value(_PKG)
        clock = freq * perf / 100.0 if freq and perf and freq > 0 and perf > 0 else None
        if clock is not None and not 100 <= clock <= 8000:
            clock = None
        power = pkg_mw / 1000.0 if pkg_mw is not None and 0 < pkg_mw < 500_000 else None
        return clock, power

    @property
    def has_power(self) -> bool:
        return _PKG in self._h

    def close(self) -> None:
        if self._pdh is not None:
            try:
                self._pdh.PdhCloseQuery(self._q)
            except Exception:
                pass
            self._pdh = None
