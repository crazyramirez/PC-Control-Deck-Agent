"""Linux hwmon (``/sys/class/hwmon``): CPU, storage and board temperatures, fans. No root needed.

Sensor names follow LibreHardwareMonitor's so the shared mapper picks them the same way:

- ``coretemp`` (Intel): "Package id 0" -> ``CPU Package``, "Core N" -> ``CPU Core #N``;
- ``k10temp`` / ``zenpower`` (AMD): Tctl -> ``Core (Tctl)``, Tdie -> ``Core (Tdie)``, TccdN -> ``CCDN (Tdie)``;
- ``cpu_thermal`` (Raspberry Pi and other ARM SoCs) -> ``CPU Package``;
- plus ``Core Max`` = hottest core / CCD;
- ``nvme`` (Composite) and ``drivetemp`` (SATA, module ``drivetemp``) -> storage temperatures named
  after the drive model;
- GPU drivers (amdgpu, nouveau, i915, xe, radeon) are skipped here: ``linux_gpu`` reads them per card;
- anything else: extra temperatures (ACPI, PCH, Wi-Fi, DIMMs, Super I/O...) and every ``fan*_input``.

Units: ``temp*_input`` is millidegrees Celsius, ``fan*_input`` RPM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .models import HardwareKind, RawSensor, SensorKind
from .sysfs import read_int, read_text, subdirs

CPU_CHIPS = ("coretemp", "k10temp", "zenpower", "cpu_thermal")
STORAGE_CHIPS = ("nvme", "drivetemp")
GPU_CHIPS = ("amdgpu", "radeon", "nouveau", "i915", "xe")
# Short, readable names for common chips (the deck shows at most 23 bytes).
FRIENDLY = (("acpitz", "ACPI"), ("pch_", "PCH"), ("iwlwifi", "Wi-Fi"), ("spd5118", "DIMM"), ("jc42", "DIMM"),
            ("thinkpad", "ThinkPad"), ("dell_smm", "Dell"), ("BAT", "Battery"))  # fmt: skip

_CHANNEL = re.compile(r"^(temp|fan)(\d+)_input$")


@dataclass
class Channel:
    kind: str  # "temp" | "fan"
    index: int
    label: str | None


@dataclass
class Chip:
    path: Path
    name: str
    occurrence: int  # 0 for the first chip with this name, 1 for the second...
    display: str  # readable chip name, unique among chips
    channels: list[Channel] = field(default_factory=list)

    def value(self, ch: Channel) -> float | None:
        raw = read_int(self.path / f"{ch.kind}{ch.index}_input")
        if raw is None:
            return None
        return raw / 1000.0 if ch.kind == "temp" else float(raw)


def _friendly(name: str) -> str:
    for prefix, nice in FRIENDLY:
        if name.startswith(prefix):
            return nice
    return name


def scan(root: Path) -> list[Chip]:
    """Discover chips and their channels once (layout does not change while running)."""
    chips: list[Chip] = []
    seen: dict[str, int] = {}
    dirs = subdirs(root / "sys" / "class" / "hwmon", "hwmon*")
    names = [read_text(d / "name") or d.name for d in dirs]
    for d, name in zip(dirs, names):
        occ = seen.get(name, 0)
        seen[name] = occ + 1
        nice = _friendly(name)
        display = nice if names.count(name) == 1 else f"{nice}#{occ + 1}"
        chip = Chip(path=d, name=name, occurrence=occ, display=display)
        try:
            files = sorted(p.name for p in d.iterdir())
        except OSError:
            files = []
        for f in files:
            m = _CHANNEL.match(f)
            if m:
                kind, idx = m.group(1), int(m.group(2))
                chip.channels.append(Channel(kind, idx, read_text(d / f"{kind}{idx}_label") or None))
        chip.channels.sort(key=lambda c: (c.kind, c.index))
        chips.append(chip)
    return chips


def _cpu_name(chip: Chip, ch: Channel) -> str | None:
    label = ch.label or ""
    if chip.name == "coretemp":
        m = re.match(r"Package id (\d+)", label)
        if m:
            return "CPU Package" if m.group(1) == "0" else f"CPU Package #{m.group(1)}"
        m = re.match(r"Core (\d+)", label)
        return f"CPU Core #{m.group(1)}" if m else None
    if chip.name in ("k10temp", "zenpower"):
        if label == "Tdie":
            return "Core (Tdie)"
        if label == "Tctl" or (not label and ch.index == 1):
            return "Core (Tctl)"
        m = re.match(r"Tccd(\d+)", label)
        return f"CCD{m.group(1)} (Tdie)" if m else None
    if chip.name == "cpu_thermal":
        return "CPU Package" if ch.index == 1 else None
    return None


def cpu_temperatures(chips: list[Chip], hw_id: str, hw_name: str) -> list[RawSensor]:
    out: list[RawSensor] = []
    cores: list[float | None] = []
    for chip in chips:
        if chip.name not in CPU_CHIPS:
            continue
        for ch in chip.channels:
            name = _cpu_name(chip, ch) if ch.kind == "temp" else None
            if name is None:
                continue
            v = chip.value(ch)
            if name.startswith(("CPU Core #", "CCD")):
                cores.append(v)
            out.append(RawSensor(f"/hwmon/{chip.name}/{chip.occurrence}/temp{ch.index}", hw_id, HardwareKind.CPU, hw_name,
                                 SensorKind.TEMPERATURE, name, v, "C"))  # fmt: skip
    if cores:
        valid = [c for c in cores if c is not None]
        out.append(RawSensor("/hwmon/cpu/coremax", hw_id, HardwareKind.CPU, hw_name, SensorKind.TEMPERATURE, "Core Max",
                             max(valid) if valid else None, "C"))  # fmt: skip
    return out


def _drive_model(chip: Chip) -> str:
    model = read_text(chip.path / "device" / "model")
    vendor = read_text(chip.path / "device" / "vendor")
    if model and vendor and vendor not in ("ATA", "NVME") and not vendor.startswith("0x"):
        model = f"{vendor} {model}"
    return " ".join(model.split()) if model else ("NVMe drive" if chip.name == "nvme" else "SATA drive")


def storage_temperatures(chips: list[Chip]) -> list[RawSensor]:
    out: list[RawSensor] = []
    for chip in chips:
        if chip.name not in STORAGE_CHIPS:
            continue
        temps = [c for c in chip.channels if c.kind == "temp"]
        main = next((c for c in temps if (c.label or "") == "Composite"), temps[0] if temps else None)
        if main is None:
            continue
        hw_id = f"/{chip.name}/{chip.occurrence}"
        out.append(RawSensor(f"{hw_id}/temp{main.index}", hw_id, HardwareKind.STORAGE, _drive_model(chip),
                             SensorKind.TEMPERATURE, "Temperature", chip.value(main), "C"))  # fmt: skip
    return out


def other_temperatures(chips: list[Chip]) -> list[RawSensor]:
    out: list[RawSensor] = []
    for chip in chips:
        if chip.name in CPU_CHIPS + STORAGE_CHIPS + GPU_CHIPS:
            continue
        temps = [c for c in chip.channels if c.kind == "temp"]
        for ch in temps:
            if ch.label:
                name = f"{chip.display} {ch.label}"
            else:
                name = chip.display if len(temps) == 1 or ch.index == 1 else f"{chip.display} {ch.index}"
            out.append(RawSensor(f"/hwmon/{chip.name}/{chip.occurrence}/temp{ch.index}", f"/hwmon/{chip.name}/{chip.occurrence}",
                                 HardwareKind.OTHER, chip.display, SensorKind.TEMPERATURE, name, chip.value(ch), "C"))  # fmt: skip
    return out


def fans(chips: list[Chip]) -> list[RawSensor]:
    """Every fan the board exposes (a stopped fan reads 0 RPM, like LibreHardwareMonitor shows it)."""
    with_fans = [c for c in chips if c.name not in GPU_CHIPS and any(ch.kind == "fan" for ch in c.channels)]
    out: list[RawSensor] = []
    for chip in with_fans:
        for ch in (c for c in chip.channels if c.kind == "fan"):
            name = ch.label or f"Fan #{ch.index}"
            if not ch.label and len(with_fans) > 1:
                name = f"{chip.display} {name}"
            out.append(RawSensor(f"/hwmon/{chip.name}/{chip.occurrence}/fan{ch.index}", f"/hwmon/{chip.name}/{chip.occurrence}",
                                 HardwareKind.OTHER, chip.display, SensorKind.FAN, name, chip.value(ch), "RPM"))  # fmt: skip
    return out
