import math

import pytest

from pcdeck_agent.hardware.catalog import build_catalog, diagnostic_report
from pcdeck_agent.hardware.fake_provider import FakeHardwareMetricsProvider, sensor
from pcdeck_agent.hardware.mapper import MapperConfig, SensorMapper, sanitize
from pcdeck_agent.hardware.models import HardwareKind, Quality, SensorKind
from pcdeck_agent.hardware.provider import OsMemory

NV = dict(hw_kind=HardwareKind.GPU_DISCRETE, hw_id="/gpu-nvidia/0", hw_name="NVIDIA Test")
IG = dict(hw_kind=HardwareKind.GPU_INTEGRATED, hw_id="/gpu-intel-integrated/x", hw_name="Intel Test")
SSD = dict(hw_kind=HardwareKind.STORAGE, hw_id="/nvme/0", hw_name="NVMe Test")
MB = dict(hw_kind=HardwareKind.SUPERIO, hw_id="/lpc/0", hw_name="SuperIO Test")
MEM = OsMemory(used_mib=24000.0, total_mib=65536.0)


def mapper(**kw):
    return SensorMapper(MapperConfig(host="test", **kw))


# ---- sanitize ---------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "kind,value,expected_q",
    [
        (SensorKind.TEMPERATURE, None, Quality.MISSING),
        (SensorKind.TEMPERATURE, math.nan, Quality.INVALID),
        (SensorKind.TEMPERATURE, math.inf, Quality.INVALID),
        (SensorKind.TEMPERATURE, -math.inf, Quality.INVALID),
        (SensorKind.TEMPERATURE, 255.0, Quality.INVALID),
        (SensorKind.TEMPERATURE, 65.5, Quality.OK),
        (SensorKind.LOAD, 101.0, Quality.INVALID),
        (SensorKind.LOAD, 0.0, Quality.OK),
        (SensorKind.FAN, -1.0, Quality.INVALID),
    ],
)
def test_sanitize(kind, value, expected_q):
    v, q = sanitize(sensor("/x", "X", kind, value))
    assert q == expected_q
    assert (v is None) == (q != Quality.OK)


def test_cpu_zero_power_is_null_not_zero():
    # LHM without MSR access reports exactly 0.0 W for CPU package.
    snap = mapper().map([sensor("/cpu/power/0", "CPU Package", SensorKind.POWER, 0.0)], MEM)
    assert snap.cpu.package_power_w is None


def test_gpu_zero_power_is_kept():
    snap = mapper().map([sensor("/g/p", "GPU Package", SensorKind.POWER, 0.0, **NV)], MEM)
    assert snap.gpu.board_power_w == 0.0


# ---- CPU selection ----------------------------------------------------------------------------
def test_cpu_temp_priority_and_traceability():
    raw = [
        sensor("/cpu/t/1", "Core Max", SensorKind.TEMPERATURE, 80.0),
        sensor("/cpu/t/0", "CPU Package", SensorKind.TEMPERATURE, 72.0),
    ]
    snap = mapper().map(raw, MEM)
    assert snap.cpu.temp_c == 72.0
    assert snap.cpu.temp_sensor_id == "/cpu/t/0"
    assert snap.cpu.core_max_temp_c == 80.0


def test_cpu_temp_amd_tctl():
    snap = mapper().map([sensor("/amdcpu/0/t/2", "Core (Tctl/Tdie)", SensorKind.TEMPERATURE, 61.0)], MEM)
    assert snap.cpu.temp_c == 61.0


def test_lenovo_ec_used_only_when_lhm_has_no_cpu_temp():
    ec = sensor("/lenovo-gamezone/cpu/temperature", "CPU (EC)", SensorKind.TEMPERATURE, 63.0, hw_id="/lenovo-gamezone", hw_name="Lenovo EC")
    lhm_null = sensor("/cpu/t/0", "CPU Package", SensorKind.TEMPERATURE, None, hw_id="/intelcpu/0", hw_name="Intel Core Ultra 9")
    snap = mapper().map([ec, lhm_null], MEM)  # EC listed first on purpose
    assert snap.cpu.temp_c == 63.0
    assert snap.cpu.temp_sensor_name == "CPU (EC)"
    assert snap.cpu.name == "Intel Core Ultra 9"  # EC pseudo-hardware never becomes the CPU name
    lhm_ok = sensor("/cpu/t/0", "CPU Package", SensorKind.TEMPERATURE, 70.0, hw_id="/intelcpu/0", hw_name="Intel Core Ultra 9")
    snap = mapper().map([ec, lhm_ok], MEM)
    assert snap.cpu.temp_c == 70.0 and snap.cpu.temp_sensor_name == "CPU Package"


def test_cpu_temp_pinned_by_config():
    raw = [
        sensor("/cpu/t/0", "CPU Package", SensorKind.TEMPERATURE, 72.0),
        sensor("/cpu/t/9", "CPU CCD1", SensorKind.TEMPERATURE, 68.0),
    ]
    snap = mapper(cpu_temp_sensor_id="/cpu/t/9").map(raw, MEM)
    assert snap.cpu.temp_c == 68.0 and snap.cpu.temp_sensor_id == "/cpu/t/9"


def test_cpu_all_temps_null_like_unprivileged_lhm():
    raw = [
        sensor("/cpu/t/0", "CPU Package", SensorKind.TEMPERATURE, None),
        sensor("/cpu/t/1", "Core Max", SensorKind.TEMPERATURE, None),
        sensor("/cpu/l/0", "CPU Total", SensorKind.LOAD, 12.5),
        sensor("/cpu/c/1", "P-Core #1", SensorKind.CLOCK, None),
    ]
    snap = mapper().map(raw, MEM, notes=["not admin"])
    assert snap.cpu.temp_c is None and snap.cpu.temp_sensor_id is None
    assert snap.cpu.core_max_temp_c is None
    assert snap.cpu.freq_mhz is None
    assert snap.cpu.load_pct == 12.5
    assert snap.notes == ["not admin"]


def test_cpu_freq_is_max_valid_core_clock():
    raw = [
        sensor("/cpu/c/1", "P-Core #1", SensorKind.CLOCK, 5200.0),
        sensor("/cpu/c/2", "E-Core #1", SensorKind.CLOCK, 4100.0),
        sensor("/cpu/c/3", "P-Core #2", SensorKind.CLOCK, math.nan),
        sensor("/cpu/c/0", "Bus Speed", SensorKind.CLOCK, 100.0),
    ]
    assert mapper().map(raw, MEM).cpu.freq_mhz == 5200.0


# ---- GPU selection ----------------------------------------------------------------------------
def test_discrete_gpu_preferred_over_integrated():
    raw = [
        sensor("/ig/t", "GPU Core", SensorKind.TEMPERATURE, 50.0, **IG),
        sensor("/nv/t", "GPU Core", SensorKind.TEMPERATURE, 47.0, **NV),
        sensor("/nv/l", "GPU Core", SensorKind.LOAD, 38.0, **NV),
        sensor("/nv/u", "GPU Memory Used", SensorKind.DATA, 1519.0, **NV),
        sensor("/nv/m", "GPU Memory Total", SensorKind.DATA, 12227.0, **NV),
    ]
    g = mapper().map(raw, MEM).gpu
    assert g.name == "NVIDIA Test" and g.hardware_id == "/gpu-nvidia/0"
    assert (g.temp_c, g.load_pct, g.vram_used_mib, g.vram_total_mib) == (47.0, 38.0, 1519.0, 12227.0)
    assert g.board_power_w is None


def test_gpu_pinned_by_config():
    raw = [sensor("/ig/t", "GPU Core", SensorKind.TEMPERATURE, 50.0, **IG), sensor("/nv/t", "GPU Core", SensorKind.TEMPERATURE, 47.0, **NV)]
    g = mapper(gpu_hardware_id="/gpu-intel-integrated/x").map(raw, MEM).gpu
    assert g.temp_c == 50.0


def test_two_discrete_gpus_deterministic():
    a = dict(hw_kind=HardwareKind.GPU_DISCRETE, hw_id="/gpu-nvidia/1", hw_name="B")
    b = dict(hw_kind=HardwareKind.GPU_DISCRETE, hw_id="/gpu-nvidia/0", hw_name="A")
    raw = [sensor("/1/t", "GPU Core", SensorKind.TEMPERATURE, 60.0, **a), sensor("/0/t", "GPU Core", SensorKind.TEMPERATURE, 40.0, **b)]
    assert mapper().map(raw, MEM).gpu.name == "A"
    assert mapper().map(list(reversed(raw)), MEM).gpu.name == "A"


def test_no_gpu_at_all():
    g = mapper().map([], MEM).gpu
    assert g.name is None and g.temp_c is None


# ---- RAM / storage / fans -----------------------------------------------------------------------
def test_ram_from_os_not_sensors():
    snap = mapper().map([], MEM)
    assert snap.ram.used_mib == 24000.0 and snap.ram.total_mib == 65536.0 and snap.ram.source == "os"


def test_ram_missing():
    assert mapper().map([], None).ram.total_mib is None


def test_no_fans_means_empty_list():
    assert mapper().map([sensor("/cpu/l", "CPU Total", SensorKind.LOAD, 5.0)], MEM).fans == []


def test_fan_without_value_stays_null():
    raw = [sensor("/lpc/0/fan/0", "Fan #1", SensorKind.FAN, 1200.0, **MB), sensor("/lpc/0/fan/1", "Fan #2", SensorKind.FAN, None, **MB)]
    fans = mapper().map(raw, MEM).fans
    assert [(f.label, f.rpm) for f in fans] == [("Fan #1", 1200.0), ("Fan #2", None)]


def test_storage_temperature():
    s = mapper().map([sensor("/nvme/0/t/0", "Temperature", SensorKind.TEMPERATURE, 41.0, **SSD)], MEM).storage
    assert len(s) == 1 and s[0].temp_c == 41.0 and s[0].name == "NVMe Test"


# ---- stale ------------------------------------------------------------------------------------
def test_stale_detection_flags_frozen_temperature():
    t = [0.0]
    m = SensorMapper(MapperConfig(stale_after_s=10), clock=lambda: t[0])
    raw = [sensor("/cpu/t/0", "CPU Package", SensorKind.TEMPERATURE, 70.0)]
    assert m.map(raw, MEM).sensors[0].quality == Quality.OK
    t[0] = 9.0
    assert m.map(raw, MEM).sensors[0].quality == Quality.OK
    t[0] = 10.5
    snap = m.map(raw, MEM)
    assert snap.sensors[0].quality == Quality.STALE
    assert snap.cpu.temp_c == 70.0  # still reported, flagged in the sensor list
    t[0] = 11.0
    raw2 = [sensor("/cpu/t/0", "CPU Package", SensorKind.TEMPERATURE, 70.5)]
    assert m.map(raw2, MEM).sensors[0].quality == Quality.OK


def test_load_is_never_stale():
    t = [0.0]
    m = SensorMapper(MapperConfig(stale_after_s=1), clock=lambda: t[0])
    raw = [sensor("/cpu/l/0", "CPU Total", SensorKind.LOAD, 0.0)]
    m.map(raw, MEM)
    t[0] = 100.0
    assert m.map(raw, MEM).cpu.load_pct == 0.0


# ---- summary list / catalog / diag --------------------------------------------------------------
def test_summary_sensors_skip_per_core():
    raw = [
        sensor("/cpu/t/0", "CPU Package", SensorKind.TEMPERATURE, 70.0),
        sensor("/cpu/t/5", "P-Core #1", SensorKind.TEMPERATURE, 71.0),
        sensor("/cpu/t/6", "P-Core #1 Distance to TjMax", SensorKind.TEMPERATURE, 29.0),
        sensor("/cpu/l/0", "CPU Total", SensorKind.LOAD, 10.0),
    ]
    ids = [s.id for s in mapper().map(raw, MEM).sensors]
    assert ids == ["/cpu/t/0"]


def test_catalog_and_redacted_diag():
    prov = FakeHardwareMetricsProvider([[sensor("/cpu/t/0", "CPU Package", SensorKind.TEMPERATURE, None)]], admin=False, notes=("needs admin",))
    raw = prov.read()
    snap = SensorMapper(MapperConfig(host="my-secret-host")).map(raw, prov.os_memory())
    cat = build_catalog(raw)
    assert cat[0].quality == "missing" and cat[0].unit == "C"
    rep = diagnostic_report(prov.status(), raw, snap)
    assert rep["snapshot"]["host"] == "<redacted>"
    assert "my-secret-host" not in str(rep)
    assert rep["is_admin"] is False and rep["notes"] == ["needs admin"]
    assert rep["sensors_by_quality"] == {"missing": 1}


def test_windows_counters_fill_missing_cpu_clock_and_power():
    from pcdeck_agent.hardware.lhm_provider import LhmProvider
    from pcdeck_agent.hardware.models import HardwareKind, RawSensor, SensorKind

    class FakePerf:
        def read(self):
            return 2450.0, 21.5

    p = LhmProvider.__new__(LhmProvider)
    p._perf = FakePerf()
    load = RawSensor(id="/cpu/0/load/0", hardware_id="/cpu/0", hardware_kind=HardwareKind.CPU, hardware_name="CPU",
                     kind=SensorKind.LOAD, name="CPU Total", value=12.0, unit="%")
    extra = p._perf_sensors([load])
    assert {(r.kind, r.value) for r in extra} == {(SensorKind.CLOCK, 2450.0), (SensorKind.POWER, 21.5)}
    assert p._perf_filled == (True, True)
    # LHM already has valid values (PawnIO installed): Windows counters are not added.
    clk = RawSensor(id="/cpu/0/clock/1", hardware_id="/cpu/0", hardware_kind=HardwareKind.CPU, hardware_name="CPU",
                    kind=SensorKind.CLOCK, name="Core #1", value=5200.0, unit="MHz")
    pwr = RawSensor(id="/cpu/0/power/0", hardware_id="/cpu/0", hardware_kind=HardwareKind.CPU, hardware_name="CPU",
                    kind=SensorKind.POWER, name="CPU Package", value=95.0, unit="W")
    assert p._perf_sensors([load, clk, pwr]) == []
