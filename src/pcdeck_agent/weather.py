"""Local weather for the deck's "Today" tab, from Open-Meteo (https://open-meteo.com).

Free, no API key, no account. The only thing that leaves the PC is what the deck owner typed in
the city search (geocoding) and the chosen place's coordinates (forecast). Results are cached so
several decks / reconnects don't multiply requests.

Payloads are compact on purpose (the deck parses them with a small JSON document):
  results  -> {"q", "results": [{"name", "region", "lat", "lon"}]}
  forecast -> {"lat", "lon", "now": {...}, "today": {...}, "hours": [...], "days": [...]}
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

log = logging.getLogger(__name__)

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
USER_AGENT = "PCControlDeck-Agent"
TIMEOUT_S = 8.0
FORECAST_TTL_S = 10 * 60
SEARCH_TTL_S = 60 * 60
MAX_RESULTS = 6
HOURS = 8
DAYS = 6
MAX_QUERY = 60

Fetch = Callable[[str], Any]  # url -> parsed JSON (injectable for tests)


class WeatherError(Exception):
    pass


def _http_json(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:  # noqa: S310 (fixed https hosts)
        return json.loads(r.read(512 * 1024).decode("utf-8"))


def _r(v: Any, nd: int = 0) -> float | int | None:
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
        return None
    return round(float(v), nd) if nd else int(round(float(v)))


def valid_coords(lat: Any, lon: Any) -> bool:
    ok = all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in (lat, lon))
    return ok and -90 <= lat <= 90 and -180 <= lon <= 180


class Weather:
    def __init__(self, fetch: Fetch | None = None, now: Callable[[], float] = time.time) -> None:
        self._fetch = fetch or _http_json
        self._now = now
        self._lock = threading.Lock()
        self._forecasts: dict[tuple[float, float], tuple[float, dict]] = {}
        self._searches: dict[tuple[str, str], tuple[float, dict]] = {}

    # ---- geocoding -----------------------------------------------------------------------------
    def search(self, query: str, lang: str = "es") -> dict[str, Any]:
        q = " ".join(str(query or "").split())[:MAX_QUERY]
        lang = lang if lang in ("es", "en") else "es"
        if len(q) < 2:
            return {"q": q, "results": []}
        key = (q.casefold(), lang)
        with self._lock:
            hit = self._searches.get(key)
            if hit and self._now() - hit[0] < SEARCH_TTL_S:
                return hit[1]
        url = GEOCODE_URL + "?" + urllib.parse.urlencode({"name": q, "count": MAX_RESULTS, "language": lang, "format": "json"})
        try:
            data = self._fetch(url)
        except Exception as e:
            raise WeatherError(type(e).__name__) from None
        out = []
        for r in (data or {}).get("results") or []:
            lat, lon = r.get("latitude"), r.get("longitude")
            if not r.get("name") or not valid_coords(lat, lon):
                continue
            region = ", ".join(x for x in (r.get("admin1"), r.get("country")) if x and x != r.get("name"))
            out.append({"name": str(r["name"])[:39], "region": region[:47], "lat": round(lat, 4), "lon": round(lon, 4)})
        res = {"q": q, "results": out[:MAX_RESULTS]}
        with self._lock:
            self._searches[key] = (self._now(), res)
        return res

    # ---- forecast ------------------------------------------------------------------------------
    def forecast(self, lat: float, lon: float) -> dict[str, Any]:
        if not valid_coords(lat, lon):
            raise WeatherError("bad_coords")
        key = (round(lat, 2), round(lon, 2))
        with self._lock:
            hit = self._forecasts.get(key)
            if hit and self._now() - hit[0] < FORECAST_TTL_S:
                return hit[1]
        params = {
            "latitude": key[0],
            "longitude": key[1],
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,is_day",
            "hourly": "temperature_2m,weather_code,precipitation_probability,is_day",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset",
            "timezone": "auto",
            "forecast_days": DAYS + 1,
        }
        url = FORECAST_URL + "?" + urllib.parse.urlencode(params)
        try:
            data = self._fetch(url)
        except Exception as e:
            raise WeatherError(type(e).__name__) from None
        res = compact(data, self._now())
        res["lat"], res["lon"] = key
        with self._lock:
            self._forecasts[key] = (self._now(), res)
        return res


def _hhmm(iso: Any) -> str:
    return iso[11:16] if isinstance(iso, str) and len(iso) >= 16 else ""


def compact(data: dict, now_epoch: float) -> dict[str, Any]:
    """Open-Meteo response -> the deck's compact forecast (times are the place's local time)."""
    if not isinstance(data, dict) or not isinstance(data.get("current"), dict):
        raise WeatherError("bad_response")
    off = int(data.get("utc_offset_seconds") or 0)
    local_now = datetime.fromtimestamp(now_epoch, timezone.utc) + timedelta(seconds=off)
    cur = data["current"]
    now = {
        "t": _r(cur.get("temperature_2m"), 1),
        "feels": _r(cur.get("apparent_temperature"), 1),
        "rh": _r(cur.get("relative_humidity_2m")),
        "wind": _r(cur.get("wind_speed_10m")),
        "code": _r(cur.get("weather_code")),
        "day": 1 if cur.get("is_day", 1) else 0,
    }
    # Hourly: the next HOURS full hours starting with the current one (place's local clock).
    hours = []
    h = data.get("hourly") or {}
    stamp = local_now.strftime("%Y-%m-%dT%H:00")
    times = h.get("time") or []
    start = next((i for i, t in enumerate(times) if t >= stamp), None)
    if start is not None:
        for i in range(start, min(start + HOURS, len(times))):

            def at(name: str, nd: int = 0, i: int = i) -> Any:
                arr = h.get(name) or []
                return _r(arr[i], nd) if i < len(arr) else None

            hours.append({"h": int(times[i][11:13]), "code": at("weather_code"), "t": at("temperature_2m"),
                          "pop": at("precipitation_probability"), "day": 1 if at("is_day") else 0})  # fmt: skip
    # Daily: today first, then the next days (weekday 0 = Monday).
    days = []
    d = data.get("daily") or {}
    today_iso = local_now.strftime("%Y-%m-%d")
    dtimes = d.get("time") or []
    for i, t in enumerate(dtimes):
        if t < today_iso:
            continue

        def dat(name: str, i: int = i) -> Any:
            arr = d.get(name) or []
            return arr[i] if i < len(arr) else None

        try:
            wd = datetime.strptime(t, "%Y-%m-%d").weekday()
        except ValueError:
            continue
        days.append({"wd": wd, "code": _r(dat("weather_code")), "max": _r(dat("temperature_2m_max")),
                     "min": _r(dat("temperature_2m_min")), "pop": _r(dat("precipitation_probability_max")),
                     "rise": _hhmm(dat("sunrise")), "set": _hhmm(dat("sunset"))})  # fmt: skip
        if len(days) >= DAYS:
            break
    today = days[0] if days else {}
    return {
        "tzMin": off // 60,
        "now": now,
        "today": {"max": today.get("max"), "min": today.get("min"), "rise": today.get("rise", ""), "set": today.get("set", "")},
        "hours": hours,
        "days": [{k: v for k, v in x.items() if k not in ("rise", "set")} for x in days[1:]],
    }


def local_clock(now: float | None = None) -> dict[str, int]:
    """The PC's clock for the deck: UTC epoch + the PC's current UTC offset (DST included)."""
    t = time.time() if now is None else now
    off = datetime.fromtimestamp(t).astimezone().utcoffset() or timedelta(0)
    return {"epoch": int(t), "utcOffsetMin": int(off.total_seconds() // 60)}
