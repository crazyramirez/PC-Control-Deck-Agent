"""Typed models shared by providers, the normalizer and (Hito 3) the protocol layer.

Rule from the spec: an unsupported or invalid metric is ``None`` (JSON null), never 0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class SensorKind(str, Enum):
    TEMPERATURE = "temperature"
    LOAD = "load"
    CLOCK = "clock"
    POWER = "power"
    FAN = "fan"
    DATA = "data"  # memory amounts
    VOLTAGE = "voltage"
    OTHER = "other"


class HardwareKind(str, Enum):
    CPU = "cpu"
    GPU_DISCRETE = "gpu-discrete"
    GPU_INTEGRATED = "gpu-integrated"
    MEMORY = "memory"
    MOTHERBOARD = "motherboard"
    SUPERIO = "superio"
    STORAGE = "storage"
    OTHER = "other"


class Quality(str, Enum):
    OK = "ok"
    MISSING = "missing"  # sensor exists but reports no value (e.g. needs admin / driver)
    INVALID = "invalid"  # NaN, inf or physically impossible value
    STALE = "stale"  # value frozen for too long


@dataclass(frozen=True)
class RawSensor:
    """One reading as delivered by a provider, before any interpretation."""

    id: str  # stable identifier, e.g. "/intelcpu/0/temperature/26"
    hardware_id: str
    hardware_kind: HardwareKind
    hardware_name: str
    kind: SensorKind
    name: str  # e.g. "CPU Package"
    value: float | None
    unit: str  # "C", "%", "MHz", "W", "RPM", "MiB", "GiB", "V", ""


@dataclass
class SensorReading:
    id: str
    name: str
    kind: SensorKind
    unit: str
    value: float | None
    quality: Quality
    hardware: str


@dataclass
class CpuMetrics:
    name: str | None = None
    temp_c: float | None = None
    temp_sensor_id: str | None = None
    temp_sensor_name: str | None = None
    core_max_temp_c: float | None = None
    load_pct: float | None = None
    freq_mhz: float | None = None
    package_power_w: float | None = None  # CPU package power, never "PC consumption"


@dataclass
class GpuMetrics:
    name: str | None = None
    hardware_id: str | None = None
    temp_c: float | None = None
    load_pct: float | None = None
    vram_used_mib: float | None = None
    vram_total_mib: float | None = None
    board_power_w: float | None = None


@dataclass
class RamMetrics:
    used_mib: float | None = None
    total_mib: float | None = None
    source: str = "os"  # "os" (psutil / Windows) preferred over sensors


@dataclass
class StorageMetrics:
    name: str
    temp_c: float | None


@dataclass
class FanMetrics:
    sensor_id: str
    label: str
    rpm: float | None


@dataclass
class TelemetrySnapshot:
    host: str
    cpu: CpuMetrics = field(default_factory=CpuMetrics)
    gpu: GpuMetrics = field(default_factory=GpuMetrics)
    ram: RamMetrics = field(default_factory=RamMetrics)
    storage: list[StorageMetrics] = field(default_factory=list)
    fans: list[FanMetrics] = field(default_factory=list)
    sensors: list[SensorReading] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # why metrics are missing (admin, driver...)
