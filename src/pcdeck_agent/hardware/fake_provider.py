"""Deterministic provider for tests and for running the agent without hardware access."""

from __future__ import annotations

from collections.abc import Callable

from .models import HardwareKind, RawSensor, SensorKind
from .provider import OsMemory, ProviderStatus


def sensor(
    id: str,
    name: str,
    kind: SensorKind,
    value: float | None,
    *,
    unit: str | None = None,
    hw_kind: HardwareKind = HardwareKind.CPU,
    hw_id: str = "/fakecpu/0",
    hw_name: str = "Fake CPU",
) -> RawSensor:
    default_units = {
        SensorKind.TEMPERATURE: "C",
        SensorKind.LOAD: "%",
        SensorKind.CLOCK: "MHz",
        SensorKind.POWER: "W",
        SensorKind.FAN: "RPM",
        SensorKind.DATA: "MiB",
        SensorKind.VOLTAGE: "V",
    }
    return RawSensor(
        id=id,
        hardware_id=hw_id,
        hardware_kind=hw_kind,
        hardware_name=hw_name,
        kind=kind,
        name=name,
        value=value,
        unit=unit if unit is not None else default_units.get(kind, ""),
    )


class FakeHardwareMetricsProvider:
    """Returns whatever ``frames`` yields; each read() advances one frame (last frame repeats)."""

    def __init__(
        self,
        frames: list[list[RawSensor]] | Callable[[int], list[RawSensor]],
        memory: OsMemory | None = OsMemory(used_mib=16384.0, total_mib=65536.0),
        admin: bool = True,
        notes: tuple[str, ...] = (),
    ) -> None:
        self._frames = frames
        self._memory = memory
        self._admin = admin
        self._notes = notes
        self._n = 0
        self.opened = False

    def open(self) -> None:
        self.opened = True

    def read(self) -> list[RawSensor]:
        if callable(self._frames):
            frame = self._frames(self._n)
        else:
            frame = self._frames[min(self._n, len(self._frames) - 1)]
        self._n += 1
        return list(frame)

    def os_memory(self) -> OsMemory | None:
        return self._memory

    def status(self) -> ProviderStatus:
        return ProviderStatus(name="fake", is_admin=self._admin, notes=self._notes)

    def close(self) -> None:
        self.opened = False
