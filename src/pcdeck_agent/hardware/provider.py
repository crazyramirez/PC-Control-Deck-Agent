"""Hardware metrics provider interface + OS-level helpers shared by all platforms."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from .models import RawSensor


@dataclass(frozen=True)
class OsMemory:
    used_mib: float
    total_mib: float


@dataclass(frozen=True)
class ProviderStatus:
    name: str
    is_admin: bool
    notes: tuple[str, ...] = ()  # human-readable limitations, e.g. "CPU temperature needs PawnIO"


class HardwareMetricsProvider(Protocol):
    """A source of raw sensor readings. Implementations: LHM (Windows), Linux (sysfs/hwmon/NVML),
    macOS (IOKit/SMC/ioreg/powermetrics), Fake (tests)."""

    def open(self) -> None: ...

    def read(self) -> list[RawSensor]:
        """Refresh hardware and return every sensor currently exposed."""
        ...

    def os_memory(self) -> OsMemory | None: ...

    def status(self) -> ProviderStatus: ...

    def close(self) -> None: ...


def psutil_memory() -> OsMemory | None:
    """System RAM from the OS (Windows: GlobalMemoryStatusEx via psutil). MiB = 2**20 bytes."""
    try:
        import psutil
    except ImportError:  # pragma: no cover
        return None
    vm = psutil.virtual_memory()
    mib = float(2**20)
    return OsMemory(used_mib=(vm.total - vm.available) / mib, total_mib=vm.total / mib)


_NAME_NOISE = re.compile(r"\((?:R|TM|tm|r)\)|\s+@\s*[\d.]+\s*GHz.*$|\s+\d+-Core Processor$|(?<=\d)\s+Processor$|\bCPU\b")


def clean_cpu_name(raw: str | None) -> str | None:
    """'Intel(R) Core(TM) i7-8700K CPU @ 3.70GHz' -> 'Intel Core i7-8700K' (LibreHardwareMonitor style)."""
    if not raw:
        return None
    s = _NAME_NOISE.sub("", " ".join(raw.split()))
    s = " ".join(s.split())
    return s or None
