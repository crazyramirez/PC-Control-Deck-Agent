"""Real-hardware smoke test. Opt-in: set PCDECK_HW_TESTS=1 (needs tools/fetch_lhm.py first)."""

import os
import sys

import pytest

pytestmark = [
    pytest.mark.hardware,
    pytest.mark.skipif(sys.platform != "win32" or os.environ.get("PCDECK_HW_TESTS") != "1", reason="opt-in hardware test"),
]


def test_lhm_reads_real_sensors_without_fake_values():
    from pcdeck_agent.hardware.lhm_provider import LhmProvider
    from pcdeck_agent.hardware.mapper import SensorMapper

    p = LhmProvider()
    p.open()
    try:
        p.read()
        raw = p.read()
        snap = SensorMapper().map(raw, p.os_memory(), p.status().notes)
    finally:
        p.close()
    assert raw, "LHM returned no sensors"
    assert snap.cpu.name
    assert snap.ram.total_mib and snap.ram.total_mib > 1024
    # Never a fabricated zero: CPU package power is either a positive reading or null.
    assert snap.cpu.package_power_w is None or snap.cpu.package_power_w > 0
    if not p.status().is_admin:
        assert p.status().notes, "missing-privilege limitations must be reported"
