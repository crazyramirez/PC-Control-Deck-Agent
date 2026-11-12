"""Linux GPUs: NVIDIA through NVML, AMD / Intel (and NVIDIA without NVML) through DRM sysfs.

- NVIDIA (``nvidia-ml-py``, needs the proprietary driver): name, temperature, load, VRAM, board power.
- AMD (``amdgpu``): ``gpu_busy_percent``, ``mem_info_vram_used/total``, hwmon edge/junction/mem
  temperatures, ``power1_average`` (or ``power1_input``), fan, shader clock. An AMD GPU whose "VRAM"
  is at most 2 GiB is taken for an APU (BIOS carve-out) and classed integrated, so a discrete card
  wins the primary-GPU choice; pin ``gpu_hardware_id`` in the config to override.
- Intel (``i915`` / ``xe``): name and current clock. Load needs the perf PMU (root) and the iGPU has
  no temperature or power of its own in sysfs: those stay null.
- NVIDIA with ``nouveau`` or without NVML: name (+ hwmon temperature when nouveau exposes it).

Names come from the PCI id database (``pci.ids``) when installed, else "AMD GPU (0x73bf)".
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from .models import HardwareKind, RawSensor, SensorKind
from .sysfs import read_int, read_text, subdirs

log = logging.getLogger(__name__)

NVIDIA, AMD, INTEL = "0x10de", "0x1002", "0x8086"
VENDOR_NAMES = {NVIDIA: "NVIDIA", AMD: "AMD", INTEL: "Intel"}
VENDOR_KEYS = {NVIDIA: "nvidia", AMD: "amd", INTEL: "intel"}
PCI_IDS = ("usr/share/hwdata/pci.ids", "usr/share/misc/pci.ids", "usr/share/pci.ids")
APU_MAX_VRAM = 2 * 2**30
MIB = float(2**20)
_INTEL_DGPU = re.compile(r"^0x(56|e2)[0-9a-f]{2}$|^0x490[5-8]$")  # Arc DG2 / Battlemage / DG1


def pci_name(root: Path, vendor: str, device: str) -> str | None:
    """Device name from pci.ids ('Navi 21 [Radeon RX 6800/6800 XT / 6900 XT]'), or None."""
    v, d = vendor.lower().removeprefix("0x"), device.lower().removeprefix("0x")
    for rel in PCI_IDS:
        p = root / rel
        try:
            with p.open(encoding="utf-8", errors="replace") as f:
                in_vendor = False
                for line in f:
                    if not line.strip() or line.startswith("#"):
                        continue
                    if not line.startswith("\t"):
                        if in_vendor:
                            return None
                        in_vendor = line[:4].lower() == v
                    elif in_vendor and not line.startswith("\t\t") and line[1:5].lower() == d:
                        return line[5:].strip()
        except OSError:
            continue
    return None


def display_name(vendor: str, raw: str | None, device: str) -> str:
    """'GA102 [GeForce RTX 3080]' -> 'NVIDIA GeForce RTX 3080'; unknown -> 'AMD GPU (0x73bf)'."""
    brand = VENDOR_NAMES.get(vendor, "GPU")
    if not raw:
        return f"{brand} GPU ({device})"
    m = re.findall(r"\[([^\]]+)\]", raw)
    name = m[-1] if m else raw
    return name if name.lower().startswith(brand.lower()) else f"{brand} {name}"


def _s(hw_id: str, kind: HardwareKind, hw_name: str, sk: SensorKind, name: str, v: float | None, unit: str, tag: str) -> RawSensor:
    return RawSensor(f"{hw_id}/{tag}", hw_id, kind, hw_name, sk, name, v, unit)


class NvmlGpus:
    """NVIDIA GPUs via NVML. ``nvml`` is the ``pynvml`` module (injectable for tests)."""

    def __init__(self, nvml: Any = None) -> None:
        self._nvml = nvml
        self._handles: list[tuple[Any, str]] = []
        self.ok = False

    def open(self) -> None:
        nv = self._nvml
        if nv is None:
            try:
                import pynvml as nv  # type: ignore[import-not-found,no-redef]  # nvidia-ml-py
            except ImportError:
                return
        try:
            nv.nvmlInit()
            for i in range(int(nv.nvmlDeviceGetCount())):
                h = nv.nvmlDeviceGetHandleByIndex(i)
                name = nv.nvmlDeviceGetName(h)
                self._handles.append((h, name.decode() if isinstance(name, bytes) else str(name)))
        except Exception as e:  # no driver / libnvidia-ml.so missing: NVMLError_LibraryNotFound
            log.info("NVML not usable: %s", type(e).__name__)
            self._handles = []
            return
        self._nvml = nv
        self.ok = bool(self._handles)

    def _get(self, fn: str, *args: Any) -> Any:
        try:
            return getattr(self._nvml, fn)(*args)
        except Exception:
            return None

    def read(self) -> list[RawSensor]:
        out: list[RawSensor] = []
        if not self.ok:
            return out
        nv = self._nvml
        for i, (h, name) in enumerate(self._handles):
            hw = f"/gpu-nvidia/{i}"
            temp = self._get("nvmlDeviceGetTemperature", h, getattr(nv, "NVML_TEMPERATURE_GPU", 0))
            util = self._get("nvmlDeviceGetUtilizationRates", h)
            mem = self._get("nvmlDeviceGetMemoryInfo", h)
            power_mw = self._get("nvmlDeviceGetPowerUsage", h)
            k = HardwareKind.GPU_DISCRETE
            out += [
                _s(hw, k, name, SensorKind.TEMPERATURE, "GPU Core", None if temp is None else float(temp), "C", "temperature/0"),
                _s(hw, k, name, SensorKind.LOAD, "GPU Core", None if util is None else float(util.gpu), "%", "load/0"),
                _s(hw, k, name, SensorKind.DATA, "GPU Memory Used", None if mem is None else mem.used / MIB, "MiB", "smalldata/1"),
                _s(hw, k, name, SensorKind.DATA, "GPU Memory Total", None if mem is None else mem.total / MIB, "MiB", "smalldata/2"),
                _s(hw, k, name, SensorKind.POWER, "GPU Package", None if power_mw is None else power_mw / 1000.0, "W", "power/0"),
            ]
        return out

    def close(self) -> None:
        if self.ok:
            self._get("nvmlShutdown")
        self.ok = False


class DrmGpus:
    """GPUs listed under /sys/class/drm/cardN (connectors such as card0-DP-1 are ignored)."""

    def __init__(self, root: Path, skip_nvidia: bool = False) -> None:
        self.root = root
        self.cards: list[tuple[Path, str, str, str]] = []  # (card dir, vendor, device id, name)
        self.nvidia_without_nvml = False
        self._skip_nvidia = skip_nvidia

    def scan(self) -> None:
        for card in subdirs(self.root / "sys" / "class" / "drm", "card*"):
            if not re.fullmatch(r"card\d+", card.name):
                continue
            vendor = (read_text(card / "device" / "vendor") or "").lower()
            device = (read_text(card / "device" / "device") or "").lower()
            if vendor not in VENDOR_NAMES or (vendor == NVIDIA and self._skip_nvidia):
                continue  # virtual adapters (VMs), or an NVIDIA card NVML already reports
            if vendor == NVIDIA:
                self.nvidia_without_nvml = True
            self.cards.append((card, vendor, device, display_name(vendor, pci_name(self.root, vendor, device), device)))

    def read(self) -> list[RawSensor]:
        out: list[RawSensor] = []
        for card, vendor, device, name in self.cards:
            dev = card / "device"
            hw = f"/gpu-{VENDOR_KEYS[vendor]}/{card.name}"
            kind = self._kind(vendor, device, dev)
            if vendor == AMD:
                busy = read_int(dev / "gpu_busy_percent")
                used, total = read_int(dev / "mem_info_vram_used"), read_int(dev / "mem_info_vram_total")
                out += [
                    _s(hw, kind, name, SensorKind.LOAD, "GPU Core", None if busy is None else float(busy), "%", "load/0"),
                    _s(hw, kind, name, SensorKind.DATA, "GPU Memory Used", None if used is None else used / MIB, "MiB", "smalldata/1"),
                    _s(hw, kind, name, SensorKind.DATA, "GPU Memory Total", None if total is None else total / MIB, "MiB", "smalldata/2"),
                ]
            elif vendor == INTEL:
                mhz = read_int(card / "gt_cur_freq_mhz") or read_int(dev / "tile0" / "gt0" / "freq0" / "cur_freq")
                out.append(_s(hw, kind, name, SensorKind.CLOCK, "GPU Core", None if mhz is None else float(mhz), "MHz", "clock/0"))
            out += self._hwmon(dev, hw, kind, name)
        return out

    @staticmethod
    def _kind(vendor: str, device: str, dev: Path) -> HardwareKind:
        if vendor == INTEL:
            return HardwareKind.GPU_DISCRETE if _INTEL_DGPU.match(device) else HardwareKind.GPU_INTEGRATED
        if vendor == AMD:
            total = read_int(dev / "mem_info_vram_total")
            if total is not None and total <= APU_MAX_VRAM:
                return HardwareKind.GPU_INTEGRATED
        return HardwareKind.GPU_DISCRETE

    @staticmethod
    def _hwmon(dev: Path, hw: str, kind: HardwareKind, name: str) -> list[RawSensor]:
        out: list[RawSensor] = []
        for hm in subdirs(dev / "hwmon", "hwmon*")[:1]:
            names = {"edge": "GPU Core", "junction": "GPU Hot Spot", "mem": "GPU Memory"}
            for i in range(1, 6):
                raw = read_int(hm / f"temp{i}_input")
                label = read_text(hm / f"temp{i}_label")
                if raw is None and label is None:
                    continue
                sensor = names.get(label or "", "GPU Core" if i == 1 else None)
                if sensor:
                    out.append(_s(hw, kind, name, SensorKind.TEMPERATURE, sensor, None if raw is None else raw / 1000.0, "C", f"temperature/{i}"))
            uw = read_int(hm / "power1_average")
            if uw is None:
                uw = read_int(hm / "power1_input")
            if uw is not None:
                out.append(_s(hw, kind, name, SensorKind.POWER, "GPU Package", uw / 1e6, "W", "power/0"))
            rpm = read_int(hm / "fan1_input")
            if rpm is not None:
                out.append(_s(hw, kind, name, SensorKind.FAN, "GPU Fan", float(rpm), "RPM", "fan/1"))
            hz = read_int(hm / "freq1_input")
            if hz is not None:
                out.append(_s(hw, kind, name, SensorKind.CLOCK, "GPU Core", hz / 1e6, "MHz", "clock/1"))
        return out
