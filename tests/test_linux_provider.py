"""Linux provider against fake sysfs/procfs trees (runs on every OS: the root path is injected)."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from pcdeck_agent import protocol as P
from pcdeck_agent.hardware import linux_provider as LP
from pcdeck_agent.hardware.linux_gpu import display_name, pci_name
from pcdeck_agent.hardware.linux_provider import LinuxProvider, Rapl, cpu_clock_mhz, cpu_model, rapl_power_w
from pcdeck_agent.hardware.mapper import MapperConfig, SensorMapper
from pcdeck_agent.hardware.models import HardwareKind
from pcdeck_agent.hardware.provider import OsMemory

MIB = 2**20
RAPL = "sys/class/powercap/intel-rapl_0"  # real name "intel-rapl:0": a colon is not allowed in Windows file names
PCI_IDS = """# pci.ids excerpt
10de  NVIDIA Corporation
\t2206  GA102 [GeForce RTX 3080]
1002  Advanced Micro Devices, Inc. [AMD/ATI]
\t73bf  Navi 21 [Radeon RX 6800/6800 XT / 6900 XT]
\t\t1002 0e3a  Radeon RX 6900 XT
\t1638  Cezanne [Radeon Vega Series / Radeon Vega Mobile Series]
8086  Intel Corporation
\t9bc4  CometLake-H GT2 [UHD Graphics]
"""


def w(root: Path, rel: str, content: object) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"{content}\n", encoding="utf-8")


def hwmon(root: Path, n: int, name: str, **attrs: object) -> Path:
    d = f"sys/class/hwmon/hwmon{n}"
    w(root, f"{d}/name", name)
    for k, v in attrs.items():
        w(root, f"{d}/{k}", v)
    return root / d


def intel_desktop(root: Path, rapl: bool = True) -> None:
    w(root, "proc/cpuinfo", "processor\t: 0\nmodel name\t: Intel(R) Core(TM) i7-8700K CPU @ 3.70GHz\ncpu MHz\t\t: 800.000\n")
    w(root, "sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq", 4300000)
    w(root, "sys/devices/system/cpu/cpu1/cpufreq/scaling_cur_freq", 800000)
    hwmon(root, 0, "coretemp", temp1_label="Package id 0", temp1_input=65000, temp2_label="Core 0", temp2_input=61000,
          temp3_label="Core 1", temp3_input=67000)
    nvme = hwmon(root, 1, "nvme", temp1_label="Composite", temp1_input=41850, temp2_label="Sensor 1", temp2_input=39850)
    w(root, str((nvme / "device" / "model").relative_to(root)), "Samsung SSD 980 PRO 1TB")
    hwmon(root, 2, "acpitz", temp1_input=27800, temp2_input=29800)
    hwmon(root, 3, "nct6798", fan1_input=1200, temp1_label="SYSTIN", temp1_input=35000)
    hwmon(root, 4, "amdgpu", temp1_label="edge", temp1_input=52000)  # handled per card, must not repeat here
    card = "sys/class/drm/card0/device"
    w(root, f"{card}/vendor", "0x1002")
    w(root, f"{card}/device", "0x73bf")
    w(root, f"{card}/gpu_busy_percent", 37)
    w(root, f"{card}/mem_info_vram_used", 1536 * MIB)
    w(root, f"{card}/mem_info_vram_total", 16368 * MIB)
    for k, v in dict(temp1_label="edge", temp1_input=52000, temp2_label="junction", temp2_input=60000, temp3_label="mem",
                     temp3_input=58000, power1_average=120_000_000, fan1_input=1450, freq1_input=2_100_000_000).items():
        w(root, f"{card}/hwmon/hwmon4/{k}", v)
    w(root, "sys/class/drm/card0-DP-1/status", "connected")  # a connector, not a GPU
    w(root, "usr/share/hwdata/pci.ids", PCI_IDS)
    if rapl:
        w(root, f"{RAPL}/name", "package-0")
        w(root, f"{RAPL}/energy_uj", 1_000_000)
        w(root, f"{RAPL}/max_energy_range_uj", 262143328850)


def provider(root: Path, clock=None, **kw) -> LinuxProvider:
    kw.setdefault("cpu_percent", lambda: 12.5)
    kw.setdefault("memory", lambda: OsMemory(used_mib=8000.0, total_mib=32000.0))
    kw.setdefault("euid", 1000)
    kw.setdefault("nvml", SimpleNamespace(nvmlInit=lambda: (_ for _ in ()).throw(OSError("no driver"))))
    return LinuxProvider(root, clock=clock or (lambda: 0.0), rapl_dir=root / RAPL, **kw)


def test_intel_desktop_snapshot(tmp_path):
    intel_desktop(tmp_path)
    t = [100.0]
    p = provider(tmp_path, clock=lambda: t[0])
    p.open()
    w(tmp_path, f"{RAPL}/energy_uj", 1_000_000 + 25_000_000)
    t[0] += 1.0
    raw = p.read()
    snap = SensorMapper(MapperConfig(host="h")).map(raw, p.os_memory(), p.status().notes)

    assert snap.cpu.name == "Intel Core i7-8700K"
    assert (snap.cpu.temp_c, snap.cpu.temp_sensor_name, snap.cpu.core_max_temp_c) == (65.0, "CPU Package", 67.0)
    assert (snap.cpu.load_pct, snap.cpu.freq_mhz) == (12.5, 4300.0)
    assert snap.cpu.package_power_w == pytest.approx(25.0)
    g = snap.gpu
    assert g.name == "AMD Radeon RX 6800/6800 XT / 6900 XT" and g.hardware_id == "/gpu-amd/card0"
    assert (g.temp_c, g.load_pct, g.vram_used_mib, g.vram_total_mib, g.board_power_w) == (52.0, 37.0, 1536.0, 16368.0, 120.0)
    assert [(s.name, s.temp_c) for s in snap.storage] == [("Samsung SSD 980 PRO 1TB", 41.85)]
    assert {(f.label, f.rpm) for f in snap.fans} == {("Fan #1", 1200.0), ("GPU Fan", 1450.0)}
    assert (snap.ram.used_mib, snap.ram.total_mib) == (8000.0, 32000.0)
    assert snap.notes == [] and p.status().is_admin is False

    payload = P.snapshot_payload(snap)
    names = [t["name"] for t in payload["temps"]]
    assert len(names) <= P.MAX_TEMPS and all(len(n.encode()) <= P.MAX_TEMP_NAME for n in names)
    assert names[:3] == ["GPU Hot Spot", "GPU Memory", "Temperature"]  # GPU extras, then the NVMe drive
    assert "ACPI" in names and not any("Core #" in n or n in ("CPU Package", "GPU Core", "Core Max") for n in names)
    assert sum(1 for r in raw if r.name == "GPU Core" and r.kind.value == "temperature") == 1  # amdgpu not repeated


def test_rapl_not_readable_gives_null_and_note(tmp_path, monkeypatch):
    intel_desktop(tmp_path)

    def denied(self):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Rapl, "_read_energy", denied)
    p = provider(tmp_path)
    p.open()
    snap = SensorMapper().map(p.read(), p.os_memory(), p.status().notes)
    assert snap.cpu.package_power_w is None
    assert p.status().notes == (LP.NOTE_RAPL_DENIED,)
    root = provider(tmp_path, euid=0)
    root.open()
    assert root.status().notes == (LP.NOTE_RAPL_ROOT_DENIED,) and root.status().is_admin


def test_no_rapl_no_hwmon_vm(tmp_path):
    w(tmp_path, "proc/cpuinfo", "model name\t: AMD EPYC 7B13\ncpu MHz\t\t: 2449.998\ncpu MHz\t\t: 2600.5\n")
    p = provider(tmp_path)
    p.open()
    snap = SensorMapper().map(p.read(), p.os_memory(), p.status().notes)
    assert snap.cpu.name == "AMD EPYC 7B13" and snap.cpu.freq_mhz == 2600.5  # /proc/cpuinfo fallback
    assert snap.cpu.temp_c is None and snap.cpu.package_power_w is None and snap.gpu.name is None
    assert snap.storage == [] and snap.fans == []
    assert p.status().notes == (LP.NOTE_RAPL_ABSENT,)


def test_rapl_delta_math():
    assert rapl_power_w(None, (5, 1.0), 100) is None
    assert rapl_power_w((1_000_000, 10.0), (31_000_000, 12.0), None) == pytest.approx(15.0)
    # wrap-around at max_energy_range_uj
    assert rapl_power_w((990_000_000, 0.0), (9_000_000, 1.0), 1_000_000_000) == pytest.approx(19.0)
    assert rapl_power_w((10, 0.0), (5, 1.0), None) is None
    assert rapl_power_w((10, 1.0), (20, 1.0), 100) is None


def test_amd_k10temp_names_and_core_max(tmp_path):
    w(tmp_path, "proc/cpuinfo", "model name\t: AMD Ryzen 9 7950X 16-Core Processor\n")
    hwmon(tmp_path, 0, "k10temp", temp1_label="Tctl", temp1_input=71250, temp3_label="Tccd1", temp3_input=66000,
          temp4_label="Tccd2", temp4_input=69500)
    p = provider(tmp_path)
    p.open()
    snap = SensorMapper().map(p.read(), None)
    assert snap.cpu.name == "AMD Ryzen 9 7950X"
    assert (snap.cpu.temp_c, snap.cpu.temp_sensor_name, snap.cpu.core_max_temp_c) == (71.25, "Core (Tctl)", 69.5)
    assert snap.ram.source == "none"
    temps = [t["name"] for t in P.snapshot_payload(snap)["temps"]]
    assert temps == ["CCD1 (Tdie)", "CCD2 (Tdie)"]


class FakeNvml:
    NVML_TEMPERATURE_GPU = 0

    def __init__(self):
        self.shutdown = False

    def nvmlInit(self):
        pass

    def nvmlDeviceGetCount(self):
        return 1

    def nvmlDeviceGetHandleByIndex(self, i):
        return f"h{i}"

    def nvmlDeviceGetName(self, h):
        return b"NVIDIA GeForce RTX 3080"  # old bindings return bytes

    def nvmlDeviceGetTemperature(self, h, sensor):
        return 55

    def nvmlDeviceGetUtilizationRates(self, h):
        return SimpleNamespace(gpu=40, memory=10)

    def nvmlDeviceGetMemoryInfo(self, h):
        return SimpleNamespace(used=2 * 2**30, total=10 * 2**30, free=8 * 2**30)

    def nvmlDeviceGetPowerUsage(self, h):
        raise RuntimeError("NVMLError_NotSupported")  # some laptop GPUs: power stays null

    def nvmlShutdown(self):
        self.shutdown = True


def nvidia_laptop(root: Path, apu_vram_mib: int = 512) -> None:
    w(root, "proc/cpuinfo", "model name\t: AMD Ryzen 7 5800H with Radeon Graphics\n")
    w(root, "usr/share/hwdata/pci.ids", PCI_IDS)
    w(root, "sys/class/drm/card0/device/vendor", "0x1002")
    w(root, "sys/class/drm/card0/device/device", "0x1638")
    w(root, "sys/class/drm/card0/device/mem_info_vram_total", apu_vram_mib * MIB)
    w(root, "sys/class/drm/card0/device/mem_info_vram_used", 100 * MIB)
    w(root, "sys/class/drm/card0/device/gpu_busy_percent", 3)
    w(root, "sys/class/drm/card1/device/vendor", "0x10de")
    w(root, "sys/class/drm/card1/device/device", "0x2206")


def test_nvml_gpu_preferred_over_apu(tmp_path):
    nvidia_laptop(tmp_path)
    nv = FakeNvml()
    p = provider(tmp_path, nvml=nv)
    p.open()
    raw = p.read()
    snap = SensorMapper().map(raw, None, p.status().notes)
    assert snap.gpu.name == "NVIDIA GeForce RTX 3080" and snap.gpu.hardware_id == "/gpu-nvidia/0"
    assert (snap.gpu.temp_c, snap.gpu.load_pct, snap.gpu.vram_used_mib, snap.gpu.vram_total_mib) == (55.0, 40.0, 2048.0, 10240.0)
    assert snap.gpu.board_power_w is None
    apu = {r.hardware_kind for r in raw if r.hardware_id == "/gpu-amd/card0"}
    assert apu == {HardwareKind.GPU_INTEGRATED}
    assert not any(r.hardware_id == "/gpu-nvidia/card1" for r in raw)  # NVML already covers that card
    assert p.status().name == "Linux hwmon/sysfs + NVML" and p.status().notes == (LP.NOTE_RAPL_ABSENT,)
    p.close()
    assert nv.shutdown


def test_nvidia_without_nvml_is_named_and_noted(tmp_path):
    nvidia_laptop(tmp_path)
    p = provider(tmp_path)
    p.open()
    raw = p.read()
    nv = [r for r in raw if r.hardware_id == "/gpu-nvidia/card1"]
    assert nv == [] or all(r.value is None for r in nv)
    assert LP.NOTE_NVIDIA in p.status().notes
    snap = SensorMapper(MapperConfig(gpu_hardware_id="/gpu-amd/card0")).map(raw, None)
    assert snap.gpu.name == "AMD Radeon Vega Series / Radeon Vega Mobile Series" and snap.gpu.load_pct == 3.0


def test_cpu_helpers(tmp_path):
    assert cpu_model(tmp_path) is None and cpu_clock_mhz(tmp_path) is None
    w(tmp_path, "proc/cpuinfo", "processor\t: 0\nHardware\t: BCM2835\nModel\t\t: Raspberry Pi 4 Model B Rev 1.4\n")
    assert cpu_model(tmp_path) == "Raspberry Pi 4 Model B Rev 1.4"


def test_pci_names(tmp_path):
    w(tmp_path, "usr/share/misc/pci.ids", PCI_IDS)
    assert pci_name(tmp_path, "0x10de", "0x2206") == "GA102 [GeForce RTX 3080]"
    assert pci_name(tmp_path, "0x1002", "0x0e3a") is None  # subsystem lines are not devices
    assert pci_name(tmp_path, "0x8086", "0xffff") is None
    assert display_name("0x8086", pci_name(tmp_path, "0x8086", "0x9bc4"), "0x9bc4") == "Intel UHD Graphics"
    assert display_name("0x1002", None, "0x73bf") == "AMD GPU (0x73bf)"
