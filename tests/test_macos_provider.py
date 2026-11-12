"""macOS provider pieces, run on every OS: parsers against sample outputs, IOKit replaced by fakes.

The files in fixtures/macos follow the formats printed by macOS 13-15 (ioreg, system_profiler -json,
powermetrics) but were written for these tests, not captured on one specific Mac: when a real
capture disagrees, add it there and fix the parser.
"""

import ctypes
import struct
from pathlib import Path

import pytest

from pcdeck_agent.hardware import macos_provider as MP
from pcdeck_agent.hardware.macos_iokit import SMCKeyData, fourcc, fourcc_str
from pcdeck_agent.hardware.macos_parsers import (
    decode_smc,
    hid_temperatures,
    parse_ioaccelerator,
    parse_powermetrics,
    parse_system_profiler_displays,
)
from pcdeck_agent.hardware.macos_provider import MacProvider, Powermetrics
from pcdeck_agent.hardware.mapper import SensorMapper
from pcdeck_agent.hardware.models import HardwareKind
from pcdeck_agent.hardware.provider import OsMemory

FIX = Path(__file__).parent / "fixtures" / "macos"


def fx(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def test_smc_struct_layout_and_decoding():
    assert ctypes.sizeof(SMCKeyData) == 80  # the AppleSMC user client rejects any other size
    assert fourcc_str(fourcc("TC0P")) == "TC0P" and fourcc("F0Ac") == 0x46304163
    assert decode_smc("sp78", bytes([0x3C, 0x80])) == 60.5
    assert decode_smc("sp78", bytes([0xFF, 0x00])) == -1.0
    assert decode_smc("fpe2", struct.pack(">H", 1800 * 4)) == 1800.0
    assert decode_smc("flt ", struct.pack("<f", 2150.5)) == 2150.5
    assert decode_smc("ui8 ", b"\x02") == 2.0
    assert decode_smc("ch8*", b"abcd") is None and decode_smc("sp78", b"") is None


def test_ioaccelerator_and_displays():
    [m1] = parse_ioaccelerator(fx("ioreg_accelerator_apple_silicon.txt"))
    assert (m1.ioclass, m1.model, m1.utilization, m1.vram_used_mib) == ("AGXAcceleratorG13G", "Apple M1", 9.0, None)
    amd, intel = parse_ioaccelerator(fx("ioreg_accelerator_intel_amd.txt"))
    assert amd.utilization == 14.0 and amd.temp_c == 48.0 and amd.vram_used_mib == pytest.approx(660.4, abs=0.1)
    assert amd.vram_used_mib + amd.vram_free_mib == pytest.approx(4096.0)
    assert intel.ioclass == "IntelAccelerator" and intel.utilization == 3.0
    assert parse_ioaccelerator("") == []
    d = parse_system_profiler_displays(fx("system_profiler_displays_intel_amd.json"))
    assert [(g.name, g.vendor, g.vram_mib) for g in d] == [("Intel UHD Graphics 630", "intel", None), ("AMD Radeon Pro 5500M", "amd", 4096.0)]
    assert [g.vendor for g in parse_system_profiler_displays(fx("system_profiler_displays_apple_silicon.json"))] == ["apple"]
    assert parse_system_profiler_displays("not json") == []


def test_powermetrics_samples():
    assert parse_powermetrics(fx("powermetrics_apple_silicon.txt")) == {"cpu_w": 0.437, "gpu_w": 0.015, "cpu_mhz": 1520.0}
    assert parse_powermetrics(fx("powermetrics_intel.txt")) == {"cpu_w": 5.21, "cpu_mhz": 2953.2}
    assert parse_powermetrics("") == {}


def test_powermetrics_stream_and_staleness():
    t = [0.0]
    pm = Powermetrics(clock=lambda: t[0])
    for line in fx("powermetrics_apple_silicon.txt").splitlines(keepends=True):
        pm.feed(line)
    assert pm.latest() == {}  # a sample is complete only when the next one starts
    pm.feed("*** Sampled system activity (next) ***\n")
    assert pm.latest()["cpu_w"] == 0.437
    t[0] = 10.0
    assert pm.latest() == {}  # powermetrics died: no stale numbers


def test_hid_sensor_classification():
    cpu, gpu = ("/cpu/0", "Apple M1 Pro"), ("/gpu-apple/0", "Apple M1 Pro", HardwareKind.GPU_INTEGRATED)
    temps = {"pACC MTR Temp Sensor0": 61.2, "pACC MTR Temp Sensor1": 64.8, "eACC MTR Temp Sensor0": 50.1,
             "GPU MTR Temp Sensor1": 44.0, "GPU MTR Temp Sensor4": 47.5, "NAND CH0 temp": 38.0,
             "gas gauge battery": 31.0, "PMU tdie1": 70.0, "PMU tcal": 51.0, "SOC MTR Temp Sensor0": -273.0}
    byname = {r.name: r for r in hid_temperatures(temps, cpu, gpu)}
    assert byname["Core Max"].value == 64.8 and "CPU Die" not in byname  # per-cluster sensors win over tdie
    assert byname["GPU Core"].value == 47.5 and byname["GPU Core"].hardware_id == "/gpu-apple/0"
    assert byname["Temperature"].hardware_kind == HardwareKind.STORAGE and byname["Battery"].value == 31.0
    assert "SoC" not in byname  # implausible values are dropped
    m1 = {r.name: r.value for r in hid_temperatures({"PMU tdie1": 48.0, "PMU tdie3": 52.5}, cpu, gpu)}
    assert m1 == {"CPU Die": 52.5}  # plain M1: die maximum, no GPU split available
    assert hid_temperatures({}, cpu, gpu) == []


class FakeHid:
    def __init__(self, temps):
        self.temps = temps

    def open(self):
        return True

    def read(self):
        return self.temps

    def close(self):
        pass


class FakeSmc:
    def __init__(self, values, ok=True):
        self.values, self.ok = values, ok

    def open(self):
        return self.ok

    def read(self, key):
        return self.values.get(key)

    def close(self):
        pass


def runner(outputs):
    calls = []

    def run(cmd, timeout=5.0):
        calls.append(cmd)
        for key, out in outputs.items():
            if key in " ".join(cmd):
                return out
        return None

    run.calls = calls
    return run


def mac(arch, outputs, **kw):
    kw.setdefault("cpu_percent", lambda: 7.5)
    kw.setdefault("memory", lambda: OsMemory(used_mib=9000.0, total_mib=16384.0))
    kw.setdefault("euid", 501)
    return MacProvider(runner(outputs), arch=arch, **kw)


def test_apple_silicon_not_root():
    p = mac("arm64", {"machdep.cpu.brand_string": "Apple M1\n", "SPDisplaysDataType": fx("system_profiler_displays_apple_silicon.json"),
                      "IOAccelerator": fx("ioreg_accelerator_apple_silicon.txt")},
            hid=FakeHid({"PMU tdie1": 45.0, "PMU tdie2": 47.25, "NAND CH0 temp": 35.0}), smc=FakeSmc({"FNum": 0.0}))
    p.open()
    raw = p.read()
    snap = SensorMapper().map(raw, p.os_memory(), p.status().notes)
    assert snap.cpu.name == "Apple M1" and snap.cpu.load_pct == 7.5
    assert (snap.cpu.temp_c, snap.cpu.temp_sensor_name) == (47.25, "CPU Die")
    assert snap.cpu.freq_mhz is None and snap.cpu.package_power_w is None  # need powermetrics (root)
    assert (snap.gpu.name, snap.gpu.load_pct, snap.gpu.vram_total_mib, snap.gpu.board_power_w) == ("Apple M1", 9.0, None, None)
    assert [(s.name, s.temp_c) for s in snap.storage] == [("Apple SSD", 35.0)] and snap.fans == []
    assert p.status().notes == (MP.NOTE_NOT_ROOT,) and not p.status().is_admin


def test_apple_silicon_root_with_powermetrics():
    class FakePm:
        def start(self):
            return True

        def latest(self):
            return {"cpu_w": 4.2, "gpu_w": 1.5, "cpu_mhz": 3204.0}

        def stop(self):
            pass

    p = mac("arm64", {"brand_string": "Apple M2 Max", "IOAccelerator": fx("ioreg_accelerator_apple_silicon.txt")}, euid=0,
            hid=FakeHid({"pACC MTR Temp Sensor2": 66.0}), smc=FakeSmc({"FNum": 2.0, "F0Ac": 1650.0, "F1Ac": None}), powermetrics=FakePm())
    p.open()
    snap = SensorMapper().map(p.read(), None, p.status().notes)
    assert (snap.cpu.temp_c, snap.cpu.freq_mhz, snap.cpu.package_power_w, snap.gpu.board_power_w) == (66.0, 3204.0, 4.2, 1.5)
    assert [(f.label, f.rpm) for f in snap.fans] == [("Fan #1", 1650.0), ("Fan #2", None)]
    assert p.status().notes == () and p.status().is_admin


def test_intel_mac_smc_and_amd_gpu():
    p = mac("x86_64", {"brand_string": "Intel(R) Core(TM) i9-9880H CPU @ 2.30GHz", "SPDisplaysDataType": fx("system_profiler_displays_intel_amd.json"),
                       "IOAccelerator": fx("ioreg_accelerator_intel_amd.txt")},
            smc=FakeSmc({"FNum": 1.0, "F0Ac": 2160.0, "TC0P": 55.0, "TC0D": None, "TC0F": 71.5, "TG0D": 50.0}))
    p.open()
    raw = p.read()
    snap = SensorMapper().map(raw, None, p.status().notes)
    assert snap.cpu.name == "Intel Core i9-9880H" and (snap.cpu.temp_c, snap.cpu.temp_sensor_name) == (71.5, "CPU Die")
    assert snap.gpu.name == "AMD Radeon Pro 5500M" and snap.gpu.hardware_id == "/gpu-amd/0"  # discrete preferred
    assert (snap.gpu.load_pct, snap.gpu.temp_c) == (14.0, 48.0)
    assert snap.gpu.vram_total_mib == pytest.approx(4096.0) and snap.gpu.vram_used_mib == pytest.approx(660.4, abs=0.1)
    assert [(f.label, f.rpm) for f in snap.fans] == [("Fan #1", 2160.0)]
    assert any(r.name == "CPU Proximity" and r.value == 55.0 for r in raw)


def test_everything_missing_degrades_to_null():
    p = mac("arm64", {}, hid=FakeHid({}), smc=FakeSmc({}, ok=False))
    p.open()
    snap = SensorMapper().map(p.read(), None, p.status().notes)
    assert snap.cpu.name == "Mac CPU" and snap.cpu.temp_c is None and snap.gpu.name is None
    assert MP.NOTE_NO_CPU_TEMP in p.status().notes
