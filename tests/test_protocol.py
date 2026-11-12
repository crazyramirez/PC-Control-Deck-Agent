import json
import math
from pathlib import Path

import pytest

from pcdeck_agent import protocol as P
from pcdeck_agent.hardware.models import CpuMetrics, FanMetrics, GpuMetrics, RamMetrics, StorageMetrics, TelemetrySnapshot

FIX = Path(__file__).resolve().parents[2] / "shared" / "fixtures"


def fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ["hello.json", "command_request.json", "snapshot_full.json", "snapshot_unprivileged.json"])
def test_valid_fixtures_decode(name):
    msg = P.decode(fixture(name))
    assert msg["v"] == 1


@pytest.mark.parametrize(
    "name,code",
    [
        ("invalid_version.json", P.E_UNSUPPORTED_VERSION),
        ("invalid_type.json", P.E_UNKNOWN_TYPE),
        ("invalid_malformed.json", P.E_MALFORMED),
    ],
)
def test_invalid_fixtures_rejected(name, code):
    with pytest.raises(P.ProtocolError) as e:
        P.decode(fixture(name))
    assert e.value.code == code


@pytest.mark.parametrize(
    "text,code",
    [
        ("[1,2]", P.E_MALFORMED),
        ('{"v":"1","type":"hello","payload":{}}', P.E_MALFORMED),
        ('{"v":true,"type":"hello","payload":{}}', P.E_MALFORMED),
        ('{"v":1,"payload":{}}', P.E_MALFORMED),
        ('{"v":1,"type":"heartbeat","payload":[]}', P.E_MALFORMED),
        ('{"v":1,"type":"hello","payload":{"clientId":""}}', P.E_MALFORMED),
        ('{"v":1,"type":"hello","payload":{"clientId":"x","protocol":2}}', P.E_UNSUPPORTED_VERSION),
    ],
)
def test_decode_rejections(text, code):
    with pytest.raises(P.ProtocolError) as e:
        P.decode(text)
    assert e.value.code == code


def test_oversized_frame_rejected():
    big = '{"v":1,"type":"heartbeat","payload":{"x":"' + "a" * P.MAX_MESSAGE_BYTES + '"}}'
    with pytest.raises(P.ProtocolError) as e:
        P.decode(big)
    assert e.value.code == P.E_TOO_LARGE


def _snap() -> TelemetrySnapshot:
    return TelemetrySnapshot(
        host="h",
        cpu=CpuMetrics(name="C" * 80, temp_c=68.24, load_pct=float("nan"), package_power_w=None, freq_mhz=5399.6),
        gpu=GpuMetrics(name="G", temp_c=math.inf, vram_used_mib=1519.4),
        ram=RamMetrics(used_mib=100.0, total_mib=200.0),
        storage=[StorageMetrics(name=f"S{i}", temp_c=40.0) for i in range(6)],
        fans=[FanMetrics(sensor_id=f"/f/{i}", label="F", rpm=None) for i in range(10)],
        notes=["n"],
    )


def test_snapshot_payload_shape_matches_fixture():
    ours = P.snapshot_payload(_snap())
    ref = json.loads(fixture("snapshot_full.json"))["payload"]
    assert set(ours) == set(ref)
    for section in ("cpu", "gpu", "ram"):
        assert set(ours[section]) == set(ref[section])
    assert set(ours["storage"][0]) == set(ref["storage"][0])
    assert set(ours["fans"][0]) == set(ref["fans"][0])


def test_snapshot_payload_nulls_rounding_and_limits():
    p = P.snapshot_payload(_snap())
    assert p["cpu"]["tempC"] == 68.2
    assert p["cpu"]["loadPct"] is None  # NaN -> null, never 0
    assert p["cpu"]["packagePowerW"] is None
    assert p["cpu"]["freqMHz"] == 5400.0
    assert p["gpu"]["tempC"] is None  # inf -> null
    assert len(p["cpu"]["name"].encode()) <= P.MAX_NAME
    assert len(p["storage"]) == P.MAX_STORAGE and len(p["fans"]) == P.MAX_FANS
    assert p["fans"][0]["rpm"] is None


def test_extra_temps_exclude_dedicated_and_invalid():
    from pcdeck_agent.hardware.models import Quality, SensorKind, SensorReading

    def rd(i, name, v, q=Quality.OK):
        return SensorReading(id=f"/x/{i}", name=name, kind=SensorKind.TEMPERATURE, unit="C", value=v, quality=q, hardware="h")

    s = TelemetrySnapshot(
        host="h",
        cpu=CpuMetrics(temp_c=70.0, temp_sensor_id="/x/0"),
        sensors=[
            rd(0, "CPU Package", 70.0),
            rd(1, "GPU Core", 50.0),
            rd(2, "GPU Memory Junction Temperature Long Name", 56.04),
            rd(3, "Missing", None, Quality.MISSING),
            rd(4, "Frozen", 40.0, Quality.STALE),
        ]
        + [rd(10 + i, f"T{i}", 30.0 + i) for i in range(10)],
    )
    temps = P.snapshot_payload(s)["temps"]
    assert temps[0] == {"name": "GPU Memory Junction Tem", "tempC": 56.0, "stale": False}
    assert temps[1] == {"name": "Frozen", "tempC": 40.0, "stale": True}
    assert all(t["name"] not in ("CPU Package", "GPU Core", "Missing") for t in temps)
    assert len(temps) == P.MAX_TEMPS


def test_name_truncation_keeps_utf8_valid():
    s = TelemetrySnapshot(host="h", cpu=CpuMetrics(name="ñ" * 40))
    name = P.snapshot_payload(s)["cpu"]["name"]
    name.encode("utf-8")  # must not raise
    assert len(name.encode("utf-8")) <= P.MAX_NAME


def test_encode_never_emits_nan():
    text = P.encode(P.envelope(P.T_SNAPSHOT, P.snapshot_payload(_snap()), device_id="d"))
    assert "NaN" not in text and "Infinity" not in text
    assert json.loads(text)["deviceId"] == "d"


def test_fit_text_keeps_supported_scripts_and_folds_the_rest():
    from pcdeck_agent.textfit import fit_text

    assert fit_text("Canción — “Live” ’95 …") == "Canción — “Live” ’95 …"
    assert fit_text("Łódź Žluťoučký Привет Việt Nam") == "Łódź Žluťoučký Привет Việt Nam"
    assert fit_text("Song - 🔥🔥") == "Song"
    assert fit_text("ﬁne ＡＢＣ") == "fine ABC"
    assert fit_text("Ά") == "Α" or fit_text("Ά") == ""  # Greek is not in the fonts
    assert fit_text("夜に駆ける") == ""
    assert fit_text("a　b") == "a b"


def test_battery_payload():
    from pcdeck_agent.hardware.battery import BatteryMetrics
    from pcdeck_agent.protocol import battery_payload

    assert battery_payload(None) is None
    assert battery_payload(BatteryMetrics()) is None  # unknown charge -> not reported
    p = battery_payload(BatteryMetrics(pct=79, plugged=False, charging=False, secs_left=5400, remaining_wh=62.71, full_wh=79.4, rate_w=-14.25))
    assert p == {"pct": 79, "plugged": False, "charging": False, "secsLeft": 5400, "remainingWh": 62.7, "fullWh": 79.4, "rateW": -14.2}
