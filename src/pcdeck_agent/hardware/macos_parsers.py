"""Pure parsers for macOS tool output and SMC / IOHID values (unit-tested on every OS).

Sources: ``ioreg -rn AppleSmartBattery``, ``ioreg -r -d 1 -w 0 -c IOAccelerator``,
``system_profiler SPDisplaysDataType -json``, ``sysctl -n machdep.cpu.brand_string``,
``powermetrics --samplers cpu_power,gpu_power``, AppleSMC key bytes, IOHID temperature sensor names.
"""

from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass
from typing import Any

from .battery import BatteryMetrics
from .models import HardwareKind, RawSensor, SensorKind

_U64 = 2**64
_PROP = re.compile(r'^[\s|]*"([^"]+)" = (.+?)\s*$')


def _scalar(raw: str) -> Any:
    if raw == "Yes":
        return True
    if raw == "No":
        return False
    if raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1]
    if re.fullmatch(r"-?\d+", raw):
        n = int(raw)
        return n - _U64 if n >= 2**63 else n  # ioreg prints negative SInt64 values as unsigned
    return raw


def parse_ioreg_props(text: str) -> dict[str, Any]:
    """Top-level ``"Key" = value`` lines of an ioreg dump (nested dicts are kept as raw text)."""
    out: dict[str, Any] = {}
    for line in text.splitlines():
        m = _PROP.match(line)
        if m and m.group(1) not in out:
            out[m.group(1)] = _scalar(m.group(2))
    return out


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def battery_from_ioreg(p: dict[str, Any]) -> BatteryMetrics | None:
    """AppleSmartBattery -> BatteryMetrics. Capacities in mAh, Voltage in mV, Amperage in mA (signed),
    TimeRemaining / AvgTimeToEmpty in minutes (65535 = still calculating)."""
    if not p or p.get("BatteryInstalled") is False:
        return None
    cur, mx = _int(p.get("CurrentCapacity")), _int(p.get("MaxCapacity"))
    raw_cur = _int(p.get("AppleRawCurrentCapacity")) or (cur if mx and mx > 100 else None)
    raw_max = _int(p.get("AppleRawMaxCapacity")) or (mx if mx and mx > 100 else None)
    if cur is not None and mx == 100:  # Apple Silicon: CurrentCapacity is already a percentage
        pct: float | None = float(cur)
    elif raw_cur is not None and raw_max:
        pct = 100.0 * raw_cur / raw_max
    else:
        return None
    volts = _int(p.get("Voltage"))
    amps = _int(p.get("InstantAmperage")) if _int(p.get("Amperage")) is None else _int(p.get("Amperage"))
    plugged = p.get("ExternalConnected") if isinstance(p.get("ExternalConnected"), bool) else None
    charging = p.get("IsCharging") if isinstance(p.get("IsCharging"), bool) else None
    secs = None
    if plugged is False:
        for key in ("TimeRemaining", "AvgTimeToEmpty"):
            mins = _int(p.get(key))
            if mins is not None and 0 < mins < 65535:
                secs = mins * 60
                break
    wh = (lambda mah: None if mah is None or not volts else round(mah * volts / 1e6, 1))  # noqa: E731
    rate = None if amps is None or not volts else round(amps * volts / 1e6, 2)
    if rate is not None and plugged and not charging and abs(rate) < 0.05:
        rate = 0.0
    return BatteryMetrics(pct=max(0, min(100, round(pct))), plugged=plugged, charging=charging, secs_left=secs,
                          remaining_wh=wh(raw_cur), full_wh=wh(raw_max), rate_w=rate)  # fmt: skip


@dataclass
class AccelStats:
    ioclass: str
    model: str | None
    utilization: float | None  # %
    vram_used_mib: float | None
    vram_free_mib: float | None
    temp_c: float | None = None  # AMD drivers on Intel Macs publish "Temperature(C)"


def parse_ioaccelerator(text: str) -> list[AccelStats]:
    """``ioreg -r -d 1 -w 0 -c IOAccelerator``: one block per GPU, with "PerformanceStatistics"."""
    out: list[AccelStats] = []
    blocks = re.split(r"^\s*\+-o ", text, flags=re.MULTILINE)[1:]
    for b in blocks:
        ioclass = (re.search(r'"IOClass" = "([^"]+)"', b) or re.match(r"(\S+)", b))
        model = re.search(r'"model" = "([^"]+)"', b) or re.search(r'"model" = <"([^"]+)">', b)
        stats = re.search(r'"PerformanceStatistics" = \{(.*?)\}\s*$', b, re.MULTILINE)
        vals = dict((k, int(v)) for k, v in re.findall(r'"([^"]+)"=(\d+)', stats.group(1))) if stats else {}
        util = vals.get("Device Utilization %", vals.get("GPU Activity(%)"))
        used, free = vals.get("vramUsedBytes"), vals.get("vramFreeBytes")
        out.append(AccelStats(
            ioclass=ioclass.group(1) if ioclass else "?",
            model=model.group(1) if model else None,
            utilization=None if util is None else float(util),
            vram_used_mib=None if used is None else used / 2**20,
            vram_free_mib=None if free is None else free / 2**20,
            temp_c=float(vals["Temperature(C)"]) if vals.get("Temperature(C)") else None,
        ))  # fmt: skip
    return out


@dataclass(frozen=True)
class DisplayGpu:
    name: str
    vendor: str  # apple | amd | nvidia | intel | ""
    vram_mib: float | None


def parse_system_profiler_displays(text: str) -> list[DisplayGpu]:
    try:
        items = json.loads(text).get("SPDisplaysDataType", [])
    except (ValueError, AttributeError):
        return []
    out = []
    for it in items:
        name = it.get("sppci_model") or it.get("_name")
        if not name:
            continue
        vend = str(it.get("spdisplays_vendor") or it.get("sppci_vendor") or "").lower()
        vendor = next((v for v in ("apple", "amd", "nvidia", "intel") if v in vend or v in name.lower()), "")
        if not vendor and ("ati" in vend or "radeon" in name.lower()):
            vendor = "amd"
        vram = None
        m = re.match(r"([\d.]+)\s*(GB|MB)", str(it.get("spdisplays_vram") or ""))  # dedicated only (not "_shared")
        if m and vendor != "apple":
            vram = float(m.group(1)) * (1024 if m.group(2) == "GB" else 1)
        out.append(DisplayGpu(name=str(name), vendor=vendor, vram_mib=vram))
    return out


def parse_powermetrics(text: str) -> dict[str, float]:
    """One powermetrics sample -> {cpu_w, gpu_w, cpu_mhz}. Apple Silicon reports "CPU Power: N mW" and
    per-cluster "HW active frequency"; Intel reports the package power and the average frequency."""
    out: dict[str, float] = {}
    m = re.search(r"^CPU Power:\s*([\d.]+)\s*mW", text, re.MULTILINE)
    if m:
        out["cpu_w"] = float(m.group(1)) / 1000.0
    else:
        m = re.search(r"package power \(CPUs\+GT\+SA\):\s*([\d.]+)\s*W", text)
        if m:
            out["cpu_w"] = float(m.group(1))
    m = re.search(r"^GPU (?:HW )?Power:\s*([\d.]+)\s*mW", text, re.MULTILINE)
    if m:
        out["gpu_w"] = float(m.group(1)) / 1000.0
    clusters = [float(x) for x in re.findall(r"^\S*-Cluster HW active frequency:\s*([\d.]+)\s*MHz", text, re.MULTILINE)]
    if clusters:
        out["cpu_mhz"] = max(clusters)
    else:  # Intel: one line per CPU (highest wins, like the mapper's "highest core clock"), else the system average
        per_cpu = re.findall(r"^CPU Average frequency as fraction of nominal:.*?\(([\d.]+)\s*Mhz\)", text, re.MULTILINE | re.IGNORECASE)
        system = re.search(r"System Average frequency as fraction of nominal:.*?\(([\d.]+)\s*Mhz\)", text, re.IGNORECASE)
        if per_cpu:
            out["cpu_mhz"] = max(float(x) for x in per_cpu)
        elif system:
            out["cpu_mhz"] = float(system.group(1))
    return out


# ---- AppleSMC -------------------------------------------------------------------------------------
def decode_smc(data_type: str, raw: bytes) -> float | None:
    """Decode an SMC value by its 4-char type (Intel Macs: sp78 / fpe2 big-endian; Apple Silicon: flt)."""
    try:
        if data_type == "sp78":
            return struct.unpack(">h", raw[:2])[0] / 256.0
        if data_type == "fpe2":
            return struct.unpack(">H", raw[:2])[0] / 4.0
        if data_type == "flt ":
            return float(struct.unpack("<f", raw[:4])[0])
        if data_type == "ui8 ":
            return float(raw[0])
        if data_type == "ui16":
            return float(struct.unpack(">H", raw[:2])[0])
        if data_type == "ui32":
            return float(struct.unpack(">I", raw[:4])[0])
    except (struct.error, IndexError):
        return None
    return None


INTEL_CPU_DIE_KEYS = ("TCXC", "TC0F", "TC0E", "TC0D")  # PECI / die, first available wins
INTEL_KEYS = INTEL_CPU_DIE_KEYS + ("TC0P", "TG0D", "TG0P")


def smc_intel_temperatures(v: dict[str, float | None], cpu: tuple[str, str], gpu: tuple[str, str, HardwareKind] | None) -> list[RawSensor]:
    """Intel Mac SMC temperatures -> RawSensors (CPU die, CPU proximity, GPU die/proximity)."""
    out: list[RawSensor] = []
    die = next((k for k in INTEL_CPU_DIE_KEYS if v.get(k) is not None), None)
    if die:
        out.append(RawSensor(f"/smc/{die}", cpu[0], HardwareKind.CPU, cpu[1], SensorKind.TEMPERATURE, "CPU Die", v[die], "C"))
    if v.get("TC0P") is not None:
        out.append(RawSensor("/smc/TC0P", cpu[0], HardwareKind.CPU, cpu[1], SensorKind.TEMPERATURE, "CPU Proximity", v["TC0P"], "C"))
    if gpu is not None:
        for key, name in (("TG0D", "GPU Core"), ("TG0P", "GPU Proximity")):
            if v.get(key) is not None:
                out.append(RawSensor(f"/smc/{key}", gpu[0], gpu[2], gpu[1], SensorKind.TEMPERATURE, name, v[key], "C"))
    return out


# ---- Apple Silicon IOHID temperature sensors ---------------------------------------------------------
_CPU_SENSOR = re.compile(r"^[pe]ACC MTR Temp Sensor\d+$")
_GPU_SENSOR = re.compile(r"^GPU MTR Temp Sensor\d+$")
_SOC_SENSOR = re.compile(r"^SOC MTR Temp Sensor\d+$")
_TDIE = re.compile(r"^PMU2? tdie\d+$")


def _plausible(vals: list[float]) -> list[float]:
    return [x for x in vals if 0.0 < x < 150.0]


def hid_temperatures(temps: dict[str, float], cpu: tuple[str, str], gpu: tuple[str, str, HardwareKind] | None) -> list[RawSensor]:
    """Apple Silicon sensors (IOHIDEventSystemClient) -> CPU / GPU maxima, SSD, battery.

    M1 Pro/Max and later name per-cluster CPU sensors ("pACC/eACC MTR Temp Sensor*") and GPU sensors;
    plain M1 only has "PMU tdie*" die sensors spread over the whole SoC (reported as "CPU Die")."""
    out: list[RawSensor] = []
    cpu_vals = _plausible([t for n, t in temps.items() if _CPU_SENSOR.match(n)])
    if cpu_vals:
        out.append(RawSensor("/hid/cpu/max", cpu[0], HardwareKind.CPU, cpu[1], SensorKind.TEMPERATURE, "Core Max", max(cpu_vals), "C"))
    else:
        tdie = _plausible([t for n, t in temps.items() if _TDIE.match(n)])
        if tdie:
            out.append(RawSensor("/hid/tdie/max", cpu[0], HardwareKind.CPU, cpu[1], SensorKind.TEMPERATURE, "CPU Die", max(tdie), "C"))
    gpu_vals = _plausible([t for n, t in temps.items() if _GPU_SENSOR.match(n)])
    if gpu_vals and gpu is not None:
        out.append(RawSensor("/hid/gpu/max", gpu[0], gpu[2], gpu[1], SensorKind.TEMPERATURE, "GPU Core", max(gpu_vals), "C"))
    soc = _plausible([t for n, t in temps.items() if _SOC_SENSOR.match(n)])
    if soc:
        out.append(RawSensor("/hid/soc/max", "/hid/soc", HardwareKind.OTHER, "SoC", SensorKind.TEMPERATURE, "SoC", max(soc), "C"))
    nand = _plausible([t for n, t in temps.items() if n.startswith("NAND")])
    if nand:
        out.append(RawSensor("/hid/nand", "/hid/nand", HardwareKind.STORAGE, "Apple SSD", SensorKind.TEMPERATURE, "Temperature", max(nand), "C"))
    batt = _plausible([t for n, t in temps.items() if n.startswith("gas gauge battery")])
    if batt:
        out.append(RawSensor("/hid/battery", "/hid/battery", HardwareKind.OTHER, "Battery", SensorKind.TEMPERATURE, "Battery", max(batt), "C"))
    return out
