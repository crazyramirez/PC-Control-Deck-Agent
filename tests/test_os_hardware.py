"""Real-hardware smoke test for Linux / macOS (Windows: test_lhm_hardware.py). Opt-in: PCDECK_HW_TESTS=1."""

import os
import sys
import time

import pytest

pytestmark = [
    pytest.mark.hardware,
    pytest.mark.skipif(sys.platform == "win32" or os.environ.get("PCDECK_HW_TESTS") != "1", reason="opt-in hardware test"),
]


def test_real_provider_reads_without_fake_values():
    from pcdeck_agent import platforms
    from pcdeck_agent.hardware.mapper import SensorMapper

    p = platforms.hardware_provider()
    p.open()
    try:
        p.read()
        time.sleep(1.0)
        raw = p.read()
        status = p.status()
        snap = SensorMapper().map(raw, p.os_memory(), status.notes)
    finally:
        p.close()
    assert snap.cpu.name and snap.cpu.load_pct is not None
    assert snap.ram.total_mib and snap.ram.total_mib > 512
    # Never a fabricated zero: power is a positive reading or null, and null comes with a reason.
    assert snap.cpu.package_power_w is None or snap.cpu.package_power_w > 0
    if snap.cpu.package_power_w is None:
        assert status.notes
    battery = platforms.battery_reader().read()
    assert battery is None or 0 <= battery.pct <= 100
