"""Windows provider backed by LibreHardwareMonitorLib (loaded through pythonnet).

Without administrator rights and the PawnIO driver, LHM 0.9.6 cannot read CPU MSRs:
CPU temperatures/clocks come back null and power sensors report 0.0. The normalizer turns
those zeros into null; ``status().notes`` explains the limitation to the user.
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading

from .lenovo_wmi import LenovoGameZone
from .lhm_loader import LhmApi, load_lhm
from .models import HardwareKind, RawSensor, SensorKind
from .provider import OsMemory, ProviderStatus, psutil_memory
from .winperf import WinPerfCpu

log = logging.getLogger(__name__)

# LHM SensorType name -> (our kind, unit). Units follow LHM's documented conventions.
_SENSOR_MAP: dict[str, tuple[SensorKind, str]] = {
    "Temperature": (SensorKind.TEMPERATURE, "C"),
    "Load": (SensorKind.LOAD, "%"),
    "Clock": (SensorKind.CLOCK, "MHz"),
    "Power": (SensorKind.POWER, "W"),
    "Fan": (SensorKind.FAN, "RPM"),
    "Voltage": (SensorKind.VOLTAGE, "V"),
    "SmallData": (SensorKind.DATA, "MiB"),  # LHM "SmallData" = MB (binary)
    "Data": (SensorKind.DATA, "GiB"),  # LHM "Data" = GB (binary)
}

_HW_MAP: dict[str, HardwareKind] = {
    "Cpu": HardwareKind.CPU,
    "GpuNvidia": HardwareKind.GPU_DISCRETE,
    "GpuAmd": HardwareKind.GPU_DISCRETE,
    "GpuIntel": HardwareKind.GPU_INTEGRATED,
    "Memory": HardwareKind.MEMORY,
    "Motherboard": HardwareKind.MOTHERBOARD,
    "SuperIO": HardwareKind.SUPERIO,
    "Storage": HardwareKind.STORAGE,
}


def is_admin() -> bool:
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def _pawnio_installed() -> bool:
    if os.name != "nt":
        return False
    import winreg

    try:
        winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services\PawnIO").Close()
        return True
    except OSError:
        return False


class LhmProvider:
    def __init__(self, api: LhmApi | None = None) -> None:
        self._api = api
        self._computer = None
        self._lock = threading.Lock()  # LHM objects are not thread-safe
        self._ec = LenovoGameZone()
        self._perf = WinPerfCpu()  # clocks/package power when LHM cannot read MSRs
        self._perf_filled = (False, False)  # (clock, power) supplied by Windows counters last read

    def open(self) -> None:
        if self._api is None:
            self._api = load_lhm()
        self._computer = self._api.open_computer()
        log.info("LibreHardwareMonitor opened (dlls: %s)", self._api.dll_dir)
        if is_admin():  # the GameZone WMI class denies access to non-elevated callers
            self._ec.open()
        self._perf.open()

    def read(self) -> list[RawSensor]:
        if self._computer is None or self._api is None:
            raise RuntimeError("provider not opened")
        out: list[RawSensor] = []
        with self._lock:
            for hw in self._api.iter_hardware(self._computer):
                hw.Update()
            for hw in self._api.iter_hardware(self._computer):
                hw_kind = _HW_MAP.get(str(hw.HardwareType), HardwareKind.OTHER)
                hw_id = str(hw.Identifier)
                hw_name = str(hw.Name)
                for s in hw.Sensors:
                    kind, unit = _SENSOR_MAP.get(str(s.SensorType), (SensorKind.OTHER, ""))
                    v = s.Value
                    out.append(
                        RawSensor(
                            id=str(s.Identifier),
                            hardware_id=hw_id,
                            hardware_kind=hw_kind,
                            hardware_name=hw_name,
                            kind=kind,
                            name=str(s.Name),
                            value=None if v is None else float(v),
                            unit=unit,
                        )
                    )
            out.extend(self._ec.read())
            out.extend(self._perf_sensors(out))
        return out

    def _perf_sensors(self, out: list[RawSensor]) -> list[RawSensor]:
        """Windows-counter CPU clock / package power, only where LHM has no valid value."""
        clock, power = self._perf.read()
        cpu = [r for r in out if r.hardware_kind == HardwareKind.CPU]
        if not cpu:
            self._perf_filled = (False, False)
            return []
        has_clock = any(r.kind == SensorKind.CLOCK and "Core" in r.name and r.value for r in cpu)
        has_power = any(r.kind == SensorKind.POWER and r.name in ("CPU Package", "Package") and r.value for r in cpu)
        hw_id, hw_name = cpu[0].hardware_id, cpu[0].hardware_name
        extra: list[RawSensor] = []
        if not has_clock and clock is not None:
            extra.append(RawSensor(id="/winperf/cpu/clock", hardware_id=hw_id, hardware_kind=HardwareKind.CPU,
                                   hardware_name=hw_name, kind=SensorKind.CLOCK, name="Core (effective, Windows)",
                                   value=clock, unit="MHz"))
        if not has_power and power is not None:
            extra.append(RawSensor(id="/winperf/cpu/power", hardware_id=hw_id, hardware_kind=HardwareKind.CPU,
                                   hardware_name=hw_name, kind=SensorKind.POWER, name="CPU Package",
                                   value=power, unit="W"))
        self._perf_filled = (not has_clock and clock is not None, not has_power and power is not None)
        return extra

    def os_memory(self) -> OsMemory | None:
        return psutil_memory()

    def status(self) -> ProviderStatus:
        admin = is_admin()
        notes: list[str] = []
        if not admin:
            notes.append("not running as administrator: CPU temperature and storage sensors are unavailable")
        if not _pawnio_installed():
            from_windows = all(self._perf_filled)
            if self._ec.available:
                notes.append(
                    "CPU temperature from the Lenovo EC; CPU clock/power from Windows counters"
                    if from_windows
                    else "CPU temperature from the Lenovo EC; CPU power/clocks need the PawnIO driver"
                )
            else:
                notes.append(
                    "PawnIO driver not installed: CPU temperature unavailable (clock/power from Windows counters)"
                    if from_windows
                    else "PawnIO driver not installed: LHM cannot read CPU MSRs (temperature/power)"
                )
        return ProviderStatus(name=f"LibreHardwareMonitor {self._api_version()}", is_admin=admin, notes=tuple(notes))

    def close(self) -> None:
        with self._lock:
            if self._computer is not None:
                self._computer.Close()
                self._computer = None
            self._perf.close()

    @staticmethod
    def _api_version() -> str:
        from .lhm_loader import LHM_VERSION

        return LHM_VERSION
