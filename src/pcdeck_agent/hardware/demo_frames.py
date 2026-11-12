"""Synthetic frames for ``--fake`` runs. Names are explicitly fake so they can't pass as real."""

from __future__ import annotations

import math

from .fake_provider import FakeHardwareMetricsProvider, sensor
from .models import HardwareKind, RawSensor, SensorKind

GPU = dict(hw_kind=HardwareKind.GPU_DISCRETE, hw_id="/fakegpu/0", hw_name="Fake GPU (synthetic)")
CPU = dict(hw_kind=HardwareKind.CPU, hw_id="/fakecpu/0", hw_name="Fake CPU (synthetic)")


def frame(n: int) -> list[RawSensor]:
    load = 30 + 25 * math.sin(n / 5)
    return [
        sensor("/fakecpu/0/temperature/0", "CPU Package", SensorKind.TEMPERATURE, 45 + load * 0.4, **CPU),
        sensor("/fakecpu/0/temperature/1", "Core Max", SensorKind.TEMPERATURE, 49 + load * 0.4, **CPU),
        sensor("/fakecpu/0/load/0", "CPU Total", SensorKind.LOAD, load, **CPU),
        sensor("/fakecpu/0/clock/1", "P-Core #1", SensorKind.CLOCK, 3000 + load * 20, **CPU),
        sensor("/fakecpu/0/power/0", "CPU Package", SensorKind.POWER, 20 + load * 1.5, **CPU),
        sensor("/fakegpu/0/temperature/0", "GPU Core", SensorKind.TEMPERATURE, 40 + load * 0.3, **GPU),
        sensor("/fakegpu/0/load/0", "GPU Core", SensorKind.LOAD, load / 2, **GPU),
        sensor("/fakegpu/0/smalldata/1", "GPU Memory Used", SensorKind.DATA, 2048, **GPU),
        sensor("/fakegpu/0/smalldata/2", "GPU Memory Total", SensorKind.DATA, 12288, **GPU),
    ]


def demo_provider() -> FakeHardwareMetricsProvider:
    return FakeHardwareMetricsProvider(frame, notes=("synthetic data (--fake)",))
