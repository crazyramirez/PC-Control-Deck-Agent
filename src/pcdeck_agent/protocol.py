"""Protocol v1 encoding/validation (see docs/PROTOCOL.md). Pure functions, no I/O."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

from .hardware.models import TelemetrySnapshot
from .textfit import fit_text
from .weather import local_clock

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 16 * 1024
MAX_NAME = 39  # deck buffers are 40 bytes incl. NUL
MAX_STORAGE = 4
MAX_FANS = 8
MAX_TEMPS = 6
MAX_TEMP_NAME = 23  # deck buffer 24 bytes incl. NUL

T_HELLO = "hello"
T_CAPABILITIES = "telemetry.capabilities"
T_SNAPSHOT = "telemetry.snapshot"
T_HEARTBEAT = "heartbeat"
T_ERROR = "error"
T_PAIR_REQUEST = "pair.request"
T_PAIR_RESULT = "pair.result"
T_AUTH_REQUEST = "auth.request"
T_AUTH_RESULT = "auth.result"
T_COMMAND_REQUEST = "command.request"
T_COMMAND_RESULT = "command.result"
T_ART_REQUEST = "media.art_request"  # deck -> agent: resend the current cover (it missed it)
T_WEATHER_SEARCH = "weather.search"  # deck -> agent: {q, lang} city search
T_WEATHER_RESULTS = "weather.results"  # agent -> deck: {q, results[]} or {q, error}
T_WEATHER_REQUEST = "weather.request"  # deck -> agent: {lat, lon} forecast for the chosen place
T_WEATHER_DATA = "weather.data"  # agent -> deck: compact forecast or {error}

KNOWN_TYPES = {
    T_HELLO, T_CAPABILITIES, T_SNAPSHOT, T_HEARTBEAT, T_ERROR,
    T_PAIR_REQUEST, T_PAIR_RESULT, T_AUTH_REQUEST, T_AUTH_RESULT, T_COMMAND_REQUEST, T_COMMAND_RESULT,
    T_ART_REQUEST, T_WEATHER_SEARCH, T_WEATHER_RESULTS, T_WEATHER_REQUEST, T_WEATHER_DATA,
}  # fmt: skip

# Error codes
E_MALFORMED = "malformed"
E_TOO_LARGE = "too_large"
E_UNSUPPORTED_VERSION = "unsupported_version"
E_UNKNOWN_TYPE = "unknown_type"
E_UNAUTHORIZED = "unauthorized"
E_EXPECTED_HELLO = "expected_hello"
E_RATE_LIMITED = "rate_limited"
E_INVALID_CODE = "invalid_code"
E_NOT_SUPPORTED = "not_supported"


class ProtocolError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(v: float | None, digits: int = 1) -> float | None:
    if v is None or math.isnan(v) or math.isinf(v):
        return None
    return round(float(v), digits)


def _name(s: str | None, limit: int = MAX_NAME) -> str | None:
    if s is None:
        return None
    b = fit_text(s).encode("utf-8")[:limit]
    return b.decode("utf-8", "ignore")  # never split a multi-byte char


# Temperatures already carried by dedicated fields; not repeated in "temps".
_DEDICATED_TEMP_NAMES = {"CPU Package", "Core Max", "GPU Core", "CPU (EC)"}


def _extra_temps(s: TelemetrySnapshot) -> list[dict[str, Any]]:
    """Additional valid temperature sensors (e.g. GPU memory junction, board, NVMe), max MAX_TEMPS."""
    out: list[dict[str, Any]] = []
    for r in s.sensors:
        if r.kind.value != "temperature" or r.value is None or r.quality.value not in ("ok", "stale"):
            continue
        if r.id == s.cpu.temp_sensor_id or r.name in _DEDICATED_TEMP_NAMES:
            continue
        out.append({"name": _name(r.name, MAX_TEMP_NAME), "tempC": _num(r.value), "stale": r.quality.value == "stale"})
        if len(out) >= MAX_TEMPS:
            break
    return out


def envelope(msg_type: str, payload: dict[str, Any], *, device_id: str | None = None, request_id: str | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"v": PROTOCOL_VERSION, "type": msg_type, "ts": now_iso(), "payload": payload}
    if device_id is not None:
        msg["deviceId"] = device_id
    if request_id is not None:
        msg["requestId"] = request_id
    return msg


def media_payload(m: Any) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """(audio, media) sections from a MediaState-like object (None-safe)."""
    audio = {"volumePct": getattr(m, "volume_pct", None), "muted": getattr(m, "muted", None)}
    if m is None or not (getattr(m, "title", None) or getattr(m, "app", None)):
        return audio, None
    art = getattr(m, "art", None)
    return audio, {
        # A title in a script the deck cannot draw (e.g. CJK) folds to "": show the player instead.
        "title": _name(m.title, 47) or _name(m.app, 47),
        "artist": _name(m.artist, 39),
        "app": _name(m.app, 23),
        "playing": m.playing,
        "posS": getattr(m, "pos_s", None),
        "durS": getattr(m, "dur_s", None),
        "canSeek": bool(getattr(m, "can_seek", False)),
        "artId": art.art_id if art is not None else 0,  # pixels arrive as binary "PCDA" frames
        "color": art.accent_hex if art is not None else None,
    }


def battery_payload(b: Any) -> dict[str, Any] | None:
    """Laptop battery (None: the PC has no battery or it cannot be read)."""
    if b is None or getattr(b, "pct", None) is None:
        return None
    return {
        "pct": int(b.pct),
        "plugged": b.plugged,
        "charging": b.charging,
        "secsLeft": b.secs_left,
        "remainingWh": _num(b.remaining_wh),
        "fullWh": _num(b.full_wh),
        "rateW": _num(b.rate_w),
    }


def snapshot_payload(s: TelemetrySnapshot, media: Any = None, battery: Any = None) -> dict[str, Any]:
    audio, media_section = media_payload(media)
    return {
        "audio": audio,
        "media": media_section,
        "battery": battery_payload(battery),
        "cpu": {
            "name": _name(s.cpu.name),
            "tempC": _num(s.cpu.temp_c),
            "tempSensor": s.cpu.temp_sensor_id,
            "tempSensorName": _name(s.cpu.temp_sensor_name, MAX_TEMP_NAME),
            "coreMaxTempC": _num(s.cpu.core_max_temp_c),
            "loadPct": _num(s.cpu.load_pct),
            "freqMHz": _num(s.cpu.freq_mhz, 0),
            "packagePowerW": _num(s.cpu.package_power_w),
        },
        "gpu": {
            "name": _name(s.gpu.name),
            "tempC": _num(s.gpu.temp_c),
            "loadPct": _num(s.gpu.load_pct),
            "vramUsedMiB": _num(s.gpu.vram_used_mib, 0),
            "vramTotalMiB": _num(s.gpu.vram_total_mib, 0),
            "boardPowerW": _num(s.gpu.board_power_w),
        },
        "ram": {"usedMiB": _num(s.ram.used_mib, 0), "totalMiB": _num(s.ram.total_mib, 0)},
        "storage": [{"name": _name(x.name), "tempC": _num(x.temp_c)} for x in s.storage[:MAX_STORAGE]],
        "fans": [{"sensorId": f.sensor_id, "label": _name(f.label), "rpm": _num(f.rpm, 0)} for f in s.fans[:MAX_FANS]],
        "temps": _extra_temps(s),
        "notes": list(s.notes),
        "clock": local_clock(),  # the PC clock: the deck shows its time and date
    }


def encode(msg: dict[str, Any]) -> str:
    text = json.dumps(msg, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(text.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ProtocolError(E_TOO_LARGE, f"outbound {msg.get('type')} exceeds {MAX_MESSAGE_BYTES} bytes")
    return text


def decode(text: str | bytes) -> dict[str, Any]:
    """Validate an inbound frame. Raises ProtocolError with a loggable reason."""
    raw = text.encode("utf-8") if isinstance(text, str) else text
    if len(raw) > MAX_MESSAGE_BYTES:
        raise ProtocolError(E_TOO_LARGE, f"frame of {len(raw)} bytes exceeds {MAX_MESSAGE_BYTES}")
    try:
        msg = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as e:
        raise ProtocolError(E_MALFORMED, f"invalid JSON: {e.__class__.__name__}") from None
    if not isinstance(msg, dict):
        raise ProtocolError(E_MALFORMED, "message is not a JSON object")
    v = msg.get("v")
    if not isinstance(v, int) or isinstance(v, bool):
        raise ProtocolError(E_MALFORMED, "missing or non-integer 'v'")
    if v != PROTOCOL_VERSION:
        raise ProtocolError(E_UNSUPPORTED_VERSION, f"protocol v{v} not supported (agent speaks v{PROTOCOL_VERSION})")
    t = msg.get("type")
    if not isinstance(t, str):
        raise ProtocolError(E_MALFORMED, "missing or non-string 'type'")
    if t not in KNOWN_TYPES:
        raise ProtocolError(E_UNKNOWN_TYPE, f"unknown type {t[:40]!r}")
    payload = msg.get("payload", {})
    if not isinstance(payload, dict):
        raise ProtocolError(E_MALFORMED, "'payload' must be an object")
    msg["payload"] = payload
    if t == T_HELLO:
        cid = payload.get("clientId")
        if not isinstance(cid, str) or not (1 <= len(cid) <= 64):
            raise ProtocolError(E_MALFORMED, "hello.clientId must be a 1..64 char string")
        if payload.get("protocol", PROTOCOL_VERSION) != PROTOCOL_VERSION:
            raise ProtocolError(E_UNSUPPORTED_VERSION, "hello.protocol mismatch")
    elif t == T_PAIR_REQUEST:
        code = payload.get("code")
        if not isinstance(code, str) or len(code) != 6 or not code.isdigit():
            raise ProtocolError(E_MALFORMED, "pair.request.code must be 6 digits")
        name = payload.get("name", "")
        if not isinstance(name, str) or len(name) > 40:
            raise ProtocolError(E_MALFORMED, "pair.request.name must be a string <= 40 chars")
    elif t == T_AUTH_REQUEST:
        token = payload.get("token")
        if not isinstance(token, str) or not (16 <= len(token) <= 128):
            raise ProtocolError(E_MALFORMED, "auth.request.token must be a 16..128 char string")
    return msg


def error_msg(code: str, message: str, device_id: str | None = None) -> dict[str, Any]:
    return envelope(T_ERROR, {"code": code, "message": message[:200]}, device_id=device_id)
