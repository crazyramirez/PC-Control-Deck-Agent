"""Turn raw provider sensors into a normalized TelemetrySnapshot.

Spec rules implemented here:
- missing / NaN / inf / out-of-range values become ``None`` with an explicit quality, never 0;
- CPU temperature selection is traceable (``cpu.temp_sensor_id``) and can be pinned by config;
- CPU package power that reads <= 0 W is treated as invalid (LHM reports 0.0 when it lacks MSR access);
- the primary GPU is chosen deterministically (discrete before integrated) or pinned by config;
- RAM comes from the OS, not from sensors;
- sensors whose value stays bit-identical for ``stale_after_s`` are flagged ``stale``.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .models import (
    CpuMetrics,
    FanMetrics,
    GpuMetrics,
    HardwareKind,
    Quality,
    RamMetrics,
    RawSensor,
    SensorKind,
    SensorReading,
    StorageMetrics,
    TelemetrySnapshot,
)
from .provider import OsMemory

# Physically plausible ranges per kind (inclusive). Outside -> INVALID.
VALID_RANGES: dict[SensorKind, tuple[float, float]] = {
    SensorKind.TEMPERATURE: (-30.0, 150.0),
    SensorKind.LOAD: (0.0, 100.0),
    SensorKind.CLOCK: (1.0, 10000.0),
    SensorKind.POWER: (0.0, 2000.0),
    SensorKind.FAN: (0.0, 25000.0),
    SensorKind.DATA: (0.0, 1.0e7),
    SensorKind.VOLTAGE: (0.0, 30.0),
}

# Only these kinds are checked for freezing; loads legitimately sit at 0 for long periods.
STALE_KINDS = {SensorKind.TEMPERATURE, SensorKind.POWER, SensorKind.CLOCK, SensorKind.FAN}

# LHM sensors first; "CPU (EC)" is the Lenovo embedded-controller reading used when MSRs are unavailable.
CPU_TEMP_PRIORITY = ("CPU Package", "Core (Tctl/Tdie)", "Tctl/Tdie", "Core (Tdie)", "Tdie", "Core (Tctl)", "Core Max", "CPU (EC)")
EC_PREFIX = "/lenovo-gamezone"
GPU_POWER_NAMES = ("GPU Package", "GPU Board Power", "GPU Power", "GPU Core")


@dataclass
class MapperConfig:
    host: str = "PC"
    cpu_temp_sensor_id: str | None = None  # pin a specific sensor (from the catalog)
    gpu_hardware_id: str | None = None  # pin the primary GPU
    stale_after_s: float = 120.0


def sanitize(raw: RawSensor) -> tuple[float | None, Quality]:
    v = raw.value
    if v is None:
        return None, Quality.MISSING
    if math.isnan(v) or math.isinf(v):
        return None, Quality.INVALID
    lo, hi = VALID_RANGES.get(raw.kind, (-math.inf, math.inf))
    if v < lo or v > hi:
        return None, Quality.INVALID
    if raw.kind == SensorKind.POWER and raw.hardware_kind == HardwareKind.CPU and v <= 0.0:
        return None, Quality.INVALID  # a running CPU never draws 0 W: no MSR access
    return v, Quality.OK


class StaleTracker:
    def __init__(self, stale_after_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._after = stale_after_s
        self._clock = clock
        self._last: dict[str, tuple[float, float]] = {}  # id -> (value, since)

    def check(self, sensor_id: str, kind: SensorKind, value: float | None) -> bool:
        if value is None or kind not in STALE_KINDS:
            self._last.pop(sensor_id, None)
            return False
        now = self._clock()
        prev = self._last.get(sensor_id)
        if prev is None or prev[0] != value:
            self._last[sensor_id] = (value, now)
            return False
        return now - prev[1] >= self._after


class SensorMapper:
    def __init__(self, config: MapperConfig | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.config = config or MapperConfig()
        self._stale = StaleTracker(self.config.stale_after_s, clock)

    # ---- public -----------------------------------------------------------------------------
    def map(self, raw: Iterable[RawSensor], memory: OsMemory | None, notes: Iterable[str] = ()) -> TelemetrySnapshot:
        readings: dict[str, tuple[RawSensor, SensorReading]] = {}
        for r in raw:
            value, quality = sanitize(r)
            if quality == Quality.OK and self._stale.check(r.id, r.kind, value):
                quality = Quality.STALE
            readings[r.id] = (
                r,
                SensorReading(
                    id=r.id, name=r.name, kind=r.kind, unit=r.unit, value=value, quality=quality, hardware=r.hardware_name
                ),
            )

        snap = TelemetrySnapshot(host=self.config.host, notes=list(notes))
        snap.cpu = self._cpu(readings)
        snap.gpu = self._gpu(readings)
        snap.ram = self._ram(memory)
        snap.storage = self._storage(readings)
        snap.fans = self._fans(readings)
        snap.sensors = self._summary_sensors(readings)
        return snap

    # ---- helpers ----------------------------------------------------------------------------
    @staticmethod
    def _value(rd: SensorReading | None) -> float | None:
        # Stale values are still reported (with quality=stale in the sensor list) so a genuinely
        # flat reading is not hidden; the UI shows the quality flag.
        if rd is None or rd.quality not in (Quality.OK, Quality.STALE):
            return None
        return rd.value

    @staticmethod
    def _find(
        readings: dict[str, tuple[RawSensor, SensorReading]],
        *,
        hw_kinds: set[HardwareKind],
        kind: SensorKind,
        names: Iterable[str],
        hw_id: str | None = None,
        require_value: bool = True,
    ) -> SensorReading | None:
        cands = [
            (r, rd)
            for r, rd in readings.values()
            if r.hardware_kind in hw_kinds and r.kind == kind and (hw_id is None or r.hardware_id == hw_id)
        ]
        for n in names:
            for r, rd in cands:
                if r.name == n and (not require_value or rd.quality in (Quality.OK, Quality.STALE)):
                    return rd
        return None

    def _cpu(self, readings: dict[str, tuple[RawSensor, SensorReading]]) -> CpuMetrics:
        cpu_hw = [r for r, _ in readings.values() if r.hardware_kind == HardwareKind.CPU and not r.hardware_id.startswith(EC_PREFIX)]
        m = CpuMetrics(name=cpu_hw[0].hardware_name if cpu_hw else None)
        cpu = {HardwareKind.CPU}

        temp: SensorReading | None = None
        pinned = self.config.cpu_temp_sensor_id
        if pinned and pinned in readings:
            temp = readings[pinned][1]
        if temp is None or self._value(temp) is None:
            temp = self._find(readings, hw_kinds=cpu, kind=SensorKind.TEMPERATURE, names=CPU_TEMP_PRIORITY)
        m.temp_c = self._value(temp)
        m.temp_sensor_id = temp.id if temp is not None and m.temp_c is not None else None
        m.temp_sensor_name = temp.name if m.temp_sensor_id else None

        m.core_max_temp_c = self._value(self._find(readings, hw_kinds=cpu, kind=SensorKind.TEMPERATURE, names=("Core Max",)))
        m.load_pct = self._value(self._find(readings, hw_kinds=cpu, kind=SensorKind.LOAD, names=("CPU Total",)))
        m.package_power_w = self._value(
            self._find(readings, hw_kinds=cpu, kind=SensorKind.POWER, names=("CPU Package", "Package"))
        )
        clocks = [
            self._value(rd)
            for r, rd in readings.values()
            if r.hardware_kind == HardwareKind.CPU and r.kind == SensorKind.CLOCK and "Core" in r.name
        ]
        valid = [c for c in clocks if c is not None]
        m.freq_mhz = max(valid) if valid else None  # highest current core clock
        return m

    def _pick_gpu(self, readings: dict[str, tuple[RawSensor, SensorReading]]) -> tuple[str, str] | None:
        gpus: dict[str, tuple[HardwareKind, str]] = {}
        for r, _ in readings.values():
            if r.hardware_kind in (HardwareKind.GPU_DISCRETE, HardwareKind.GPU_INTEGRATED):
                gpus.setdefault(r.hardware_id, (r.hardware_kind, r.hardware_name))
        if not gpus:
            return None
        pinned = self.config.gpu_hardware_id
        if pinned and pinned in gpus:
            return pinned, gpus[pinned][1]
        # Discrete first, then integrated; stable order by id within a class.
        ordered = sorted(gpus.items(), key=lambda kv: (kv[1][0] != HardwareKind.GPU_DISCRETE, kv[0]))
        hw_id, (_, name) = ordered[0]
        return hw_id, name

    def _gpu(self, readings: dict[str, tuple[RawSensor, SensorReading]]) -> GpuMetrics:
        picked = self._pick_gpu(readings)
        if picked is None:
            return GpuMetrics()
        hw_id, name = picked
        g = {HardwareKind.GPU_DISCRETE, HardwareKind.GPU_INTEGRATED}
        f = lambda kind, names: self._value(self._find(readings, hw_kinds=g, kind=kind, names=names, hw_id=hw_id))  # noqa: E731
        return GpuMetrics(
            name=name,
            hardware_id=hw_id,
            temp_c=f(SensorKind.TEMPERATURE, ("GPU Core", "GPU Hot Spot")),
            load_pct=f(SensorKind.LOAD, ("GPU Core", "D3D 3D")),
            vram_used_mib=f(SensorKind.DATA, ("GPU Memory Used", "D3D Dedicated Memory Used")),
            vram_total_mib=f(SensorKind.DATA, ("GPU Memory Total",)),
            board_power_w=f(SensorKind.POWER, GPU_POWER_NAMES),
        )

    @staticmethod
    def _ram(memory: OsMemory | None) -> RamMetrics:
        if memory is None or memory.total_mib <= 0:
            return RamMetrics(source="none")
        return RamMetrics(used_mib=memory.used_mib, total_mib=memory.total_mib, source="os")

    def _storage(self, readings: dict[str, tuple[RawSensor, SensorReading]]) -> list[StorageMetrics]:
        out: dict[str, StorageMetrics] = {}
        for r, rd in readings.values():
            if r.hardware_kind == HardwareKind.STORAGE and r.kind == SensorKind.TEMPERATURE:
                if r.hardware_id not in out or (out[r.hardware_id].temp_c is None and self._value(rd) is not None):
                    out[r.hardware_id] = StorageMetrics(name=r.hardware_name, temp_c=self._value(rd))
        return list(out.values())

    def _fans(self, readings: dict[str, tuple[RawSensor, SensorReading]]) -> list[FanMetrics]:
        # Only fans the hardware actually exposes; a missing reading stays None (no invented RPM).
        return [FanMetrics(sensor_id=r.id, label=r.name, rpm=self._value(rd)) for r, rd in readings.values() if r.kind == SensorKind.FAN]

    @staticmethod
    def _summary_sensors(readings: dict[str, tuple[RawSensor, SensorReading]]) -> list[SensorReading]:
        """Compact list for the protocol: non-per-core temperatures, fans and power sensors."""
        out = []
        for r, rd in readings.values():
            if r.kind not in (SensorKind.TEMPERATURE, SensorKind.FAN, SensorKind.POWER):
                continue
            if "Core #" in r.name or "Distance to TjMax" in r.name:
                continue
            out.append(rd)
        return out
