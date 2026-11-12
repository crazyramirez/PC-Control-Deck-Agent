"""Hardware metrics provider interface + OS-level helpers shared by all platforms."""

from __future__ import annotations

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
    """A source of raw sensor readings. Implementations: LHM (Windows), Fake (tests), macOS (later)."""

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
