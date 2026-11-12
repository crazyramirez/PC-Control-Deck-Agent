"""Linux (sysfs power_supply) and macOS (ioreg AppleSmartBattery) battery readers."""

from pathlib import Path

from pcdeck_agent import protocol as P
from pcdeck_agent.hardware.battery import BatteryMetrics
from pcdeck_agent.hardware.linux_battery import LinuxBatteryReader, battery_from_sysfs
from pcdeck_agent.hardware.macos_battery import MacBatteryReader
from pcdeck_agent.hardware.macos_parsers import battery_from_ioreg, parse_ioreg_props

FIX = Path(__file__).parent / "fixtures" / "macos"


def supply(root: Path, name: str, **attrs: object) -> None:
    d = root / "sys" / "class" / "power_supply" / name
    d.mkdir(parents=True, exist_ok=True)
    for k, v in attrs.items():
        (d / k).write_text(f"{v}\n", encoding="utf-8")


def test_linux_energy_battery_discharging(tmp_path):
    supply(tmp_path, "AC", type="Mains", online=0)
    supply(tmp_path, "BAT0", type="Battery", present=1, status="Discharging", capacity=79, energy_now=62_710_000,
           energy_full=79_400_000, power_now=14_250_000, voltage_now=11_900_000)
    supply(tmp_path, "hidpp_battery_0", type="Battery", scope="Device", capacity=10, status="Discharging")  # a mouse
    b = LinuxBatteryReader(tmp_path).read()
    assert b == BatteryMetrics(pct=79, plugged=False, charging=False, secs_left=15842, remaining_wh=62.7, full_wh=79.4, rate_w=-14.25)
    assert P.battery_payload(b)["rateW"] == -14.2


def test_linux_charge_battery_charging_on_usb_c(tmp_path):
    supply(tmp_path, "ucsi-source-psy-USBC000-001", type="USB", online=1)  # real name has a colon (not allowed on Windows)
    supply(tmp_path, "BAT1", type="Battery", status="Charging", capacity=55, charge_now=2_750_000, charge_full=5_000_000,
           voltage_min_design=15_400_000, voltage_now=16_100_000, current_now=-2_000_000)
    b = LinuxBatteryReader(tmp_path).read()
    assert (b.pct, b.plugged, b.charging, b.secs_left) == (55, True, True, None)
    assert (b.remaining_wh, b.full_wh) == (42.4, 77.0)  # charge × design voltage
    assert b.rate_w == 32.2  # |current| × voltage_now, sign from "Charging"


def test_linux_two_batteries_full_and_idle(tmp_path):
    supply(tmp_path, "AC", type="Mains", online=1)
    supply(tmp_path, "BAT0", type="Battery", status="Full", capacity=100, energy_now=23_000_000, energy_full=23_000_000, power_now=0)
    supply(tmp_path, "BAT1", type="Battery", status="Not charging", capacity=80, energy_now=56_000_000, energy_full=70_000_000)
    b = LinuxBatteryReader(tmp_path).read()
    assert (b.pct, b.plugged, b.charging, b.rate_w, b.secs_left) == (85, True, False, 0.0, None)
    assert (b.remaining_wh, b.full_wh) == (79.0, 93.0)


def test_linux_virtual_battery_reports_no_synthetic_energy(tmp_path):
    # WSL / Hyper-V: the percentage mirrors the host, energy figures are made up (always 5 Wh full)
    supply(tmp_path, "AC1", type="Mains", online=1)
    supply(tmp_path, "BAT1", type="Battery", status="Not charging", capacity=79, energy_now=3950000, energy_full=5000000,
           power_now=0, voltage_now=5000000, model_name="Microsoft Hyper-V Virtual Battery")
    b = LinuxBatteryReader(tmp_path).read()
    assert b == BatteryMetrics(pct=79, plugged=True, charging=False, secs_left=None, remaining_wh=None, full_wh=None, rate_w=0.0)


def test_linux_no_battery(tmp_path):
    supply(tmp_path, "AC", type="Mains", online=1)
    assert LinuxBatteryReader(tmp_path).read() is None
    assert LinuxBatteryReader(tmp_path / "missing").read() is None
    assert battery_from_sysfs([{"type": "Battery", "status": "Unknown"}], []) is None  # no charge at all


def test_macos_ioreg_apple_silicon():
    props = parse_ioreg_props((FIX / "ioreg_battery_apple_silicon.txt").read_text())
    assert props["Amperage"] == -1566 and props["ExternalConnected"] is False and props["Voltage"] == 12420
    b = battery_from_ioreg(props)
    assert b == BatteryMetrics(pct=78, plugged=False, charging=False, secs_left=312 * 60, remaining_wh=48.3, full_wh=61.9, rate_w=-19.45)


def test_macos_ioreg_intel_charging():
    b = battery_from_ioreg(parse_ioreg_props((FIX / "ioreg_battery_intel.txt").read_text()))
    assert (b.pct, b.plugged, b.charging, b.secs_left) == (51, True, True, None)
    assert (b.remaining_wh, b.full_wh, b.rate_w) == (49.0, 96.5, 22.22)


def test_macos_reader_fallback_and_cache():
    calls = []
    t = [0.0]
    r = MacBatteryReader(ioreg=lambda: calls.append(1) or "", fallback=lambda: BatteryMetrics(pct=50, plugged=True), clock=lambda: t[0])
    assert r.read().pct == 50 and r.read().pct == 50 and len(calls) == 1  # cached between refreshes
    t[0] = 10.0
    r.read()
    assert len(calls) == 2
    assert battery_from_ioreg({}) is None and battery_from_ioreg({"BatteryInstalled": False}) is None
