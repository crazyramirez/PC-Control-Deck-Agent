"""Linux laptop battery from ``/sys/class/power_supply`` (no root needed).

Same fields as the Windows reader (``hardware/battery.py``):
- ``pct``: ``capacity`` (or energy/charge now ÷ full); several batteries (e.g. ThinkPad BAT0 + BAT1)
  are combined by energy;
- ``plugged``: any mains / USB-C adapter ``online`` (else inferred from the battery status);
- ``charging``: status "Charging";
- ``remaining_wh`` / ``full_wh``: ``energy_now/full`` (µWh) or ``charge_now/full`` (µAh) × the
  design voltage (as UPower does);
- ``rate_w``: ``power_now`` (µW) or ``current_now`` × ``voltage_now``; drivers disagree on the sign,
  so it comes from the status: > 0 charging, < 0 discharging, 0 when idle on AC;
- ``secs_left``: only on battery: ``time_to_empty_now`` or remaining energy ÷ rate.
Peripheral batteries (mouse, headset: ``scope=Device``) are ignored. No battery -> None.
Virtual batteries (Hyper-V / WSL "Virtual Battery") mirror the host's percentage but synthesize their
energy figures (always 5 Wh): only %, plugged and charging are reported for them.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .battery import BatteryMetrics
from .sysfs import read_text, subdirs

log = logging.getLogger(__name__)

ADAPTER_TYPES = ("Mains", "USB", "USB_C", "USB_PD", "USB_PD_DRP")
ATTRS = ("type", "scope", "present", "online", "status", "capacity", "energy_now", "energy_full", "charge_now",
         "charge_full", "voltage_now", "voltage_min_design", "voltage_max_design", "power_now", "current_now",
         "time_to_empty_now", "model_name")  # fmt: skip


def read_supplies(root: Path) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """(batteries, adapters) as {attribute: text} dicts."""
    bats: list[dict[str, str]] = []
    adapters: list[dict[str, str]] = []
    for d in subdirs(root / "sys" / "class" / "power_supply", "*"):
        attrs = {a: v for a in ATTRS if (v := read_text(d / a)) is not None}
        if attrs.get("scope") == "Device":
            continue
        kind = attrs.get("type", "")
        if kind == "Battery" and attrs.get("present", "1") != "0":
            bats.append(attrs)
        elif kind in ADAPTER_TYPES:
            adapters.append(attrs)
    return bats, adapters


def _num(attrs: dict[str, str], key: str) -> float | None:
    try:
        return float(attrs[key])
    except (KeyError, ValueError):
        return None


def _energy_wh(b: dict[str, str]) -> tuple[float | None, float | None]:
    now, full = _num(b, "energy_now"), _num(b, "energy_full")
    if now is not None or full is not None:
        return (None if now is None else now / 1e6, None if full is None else full / 1e6)
    volts = _num(b, "voltage_min_design") or _num(b, "voltage_max_design") or _num(b, "voltage_now")
    cnow, cfull = _num(b, "charge_now"), _num(b, "charge_full")
    if not volts:
        return None, None
    return (None if cnow is None else cnow * volts / 1e12, None if cfull is None else cfull * volts / 1e12)


def _power_w(b: dict[str, str]) -> float | None:
    p = _num(b, "power_now")
    if p is not None:
        return abs(p) / 1e6
    i, v = _num(b, "current_now"), _num(b, "voltage_now")
    return abs(i) * v / 1e12 if i is not None and v is not None else None


def _virtual(b: dict[str, str]) -> bool:
    return "virtual" in b.get("model_name", "").lower()


def battery_from_sysfs(bats: list[dict[str, str]], adapters: list[dict[str, str]]) -> BatteryMetrics | None:
    if not bats:
        return None
    if any(_virtual(b) for b in bats):  # keep only the mirrored percentage / status, never synthetic energy
        keep = ("type", "status", "capacity", "present")
        bats = [{k: v for k, v in b.items() if k in keep} for b in bats]
    statuses = [b.get("status", "Unknown") for b in bats]
    charging = "Charging" in statuses
    discharging = "Discharging" in statuses
    if adapters:
        plugged: bool | None = any(a.get("online") == "1" for a in adapters)
    elif charging or "Full" in statuses or "Not charging" in statuses:
        plugged = True
    else:
        plugged = False if discharging else None

    energies = [_energy_wh(b) for b in bats]
    nows = [e[0] for e in energies]
    fulls = [e[1] for e in energies]
    remaining = sum(nows) if all(n is not None for n in nows) else None  # type: ignore[arg-type]
    full = sum(fulls) if all(f is not None for f in fulls) else None  # type: ignore[arg-type]

    caps = [_num(b, "capacity") for b in bats]
    if remaining is not None and full:
        pct: float | None = 100.0 * remaining / full if len(bats) > 1 or caps[0] is None else caps[0]
    else:
        valid = [c for c in caps if c is not None]
        pct = sum(valid) / len(valid) if valid else None
    if pct is None:
        return None

    powers = [_power_w(b) for b in bats]
    power = sum(p for p in powers if p is not None) if any(p is not None for p in powers) else None
    rate: float | None
    if discharging and not charging:
        rate = None if power is None else -power
    elif charging:
        rate = power
    else:
        rate = 0.0 if plugged else None

    secs: int | None = None
    if plugged is False or (plugged is None and discharging):
        tte = [_num(b, "time_to_empty_now") for b in bats]
        if all(t is not None and t > 0 for t in tte):
            secs = int(sum(tte))  # type: ignore[arg-type]
        elif remaining is not None and power:
            secs = int(remaining / power * 3600)
        plugged = False if plugged is None else plugged
    return BatteryMetrics(
        pct=max(0, min(100, round(pct))),
        plugged=plugged,
        charging=None if all(st == "Unknown" for st in statuses) else charging,
        secs_left=secs,
        remaining_wh=None if remaining is None else round(remaining, 1),
        full_wh=None if full is None else round(full, 1),
        rate_w=None if rate is None else round(rate, 2),
    )


class LinuxBatteryReader:
    def __init__(self, root: Path | str = "/") -> None:
        self.root = Path(root)

    def read(self) -> BatteryMetrics | None:
        try:
            return battery_from_sysfs(*read_supplies(self.root))
        except Exception:
            log.debug("battery read failed", exc_info=True)
            return None
