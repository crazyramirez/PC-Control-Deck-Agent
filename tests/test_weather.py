"""Weather (Open-Meteo) compaction, caching and the server messages, with canned responses."""

import asyncio
import json
from datetime import datetime, timezone

import pytest
from websockets.asyncio.client import connect

from pcdeck_agent import protocol as P
from pcdeck_agent.hardware.demo_frames import demo_provider
from pcdeck_agent.server import AgentServer
from pcdeck_agent.weather import Weather, WeatherError, compact, local_clock

NOW = datetime(2026, 11, 12, 14, 20, tzinfo=timezone.utc).timestamp()  # 15:20 in Madrid (UTC+1)


def forecast_json():
    hours = [f"2026-11-12T{h:02d}:00" for h in range(24)] + [f"2026-11-13T{h:02d}:00" for h in range(24)]
    return {
        "utc_offset_seconds": 3600,
        "current": {"temperature_2m": 18.24, "apparent_temperature": 17.0, "relative_humidity_2m": 61.6,
                    "weather_code": 2, "wind_speed_10m": 12.4, "is_day": 1},  # fmt: skip
        "hourly": {
            "time": hours,
            "temperature_2m": [10 + i % 12 for i in range(48)],
            "weather_code": [1] * 48,
            "precipitation_probability": [5] * 48,
            "is_day": [1 if 8 <= i % 24 < 18 else 0 for i in range(48)],
        },
        "daily": {
            "time": [f"2026-11-{d}" for d in range(12, 19)],
            "weather_code": [2, 61, 3, 0, 1, 95, 71],
            "temperature_2m_max": [21.4, 17, 16, 19, 20, 15, 8],
            "temperature_2m_min": [9.2, 8, 7, 6, 7, 5, -1],
            "precipitation_probability_max": [10, 80, 20, 0, 5, 90, 60],
            "sunrise": [f"2026-11-{d}T07:58" for d in range(12, 19)],
            "sunset": [f"2026-11-{d}T17:56" for d in range(12, 19)],
        },
    }


def test_compact_forecast_starts_at_the_current_local_hour():
    out = compact(forecast_json(), NOW)
    assert out["tzMin"] == 60
    assert out["now"] == {"t": 18.2, "feels": 17.0, "rh": 62, "wind": 12, "code": 2, "day": 1}
    assert [h["h"] for h in out["hours"]] == [15, 16, 17, 18, 19, 20, 21, 22]
    assert out["hours"][3]["day"] == 0  # 18:00 is after sunset in the canned data
    assert out["today"] == {"max": 21, "min": 9, "rise": "07:58", "set": "17:56"}
    assert len(out["days"]) == 5 and out["days"][0] == {"wd": 4, "code": 61, "max": 17, "min": 8, "pop": 80}
    assert len(P.encode(P.envelope(P.T_WEATHER_DATA, out))) < 2048  # small enough for the deck


def test_forecast_is_cached_and_validates_coords():
    calls = []

    def fetch(url):
        calls.append(url)
        return forecast_json()

    t = [NOW]
    w = Weather(fetch=fetch, now=lambda: t[0])
    a = w.forecast(40.4168, -3.7038)
    assert "latitude=40.42" in calls[0] and "timezone=auto" in calls[0]
    assert w.forecast(40.4170, -3.7041) is a and len(calls) == 1  # same place (2 decimals), cached
    t[0] += 11 * 60
    w.forecast(40.4168, -3.7038)
    assert len(calls) == 2
    with pytest.raises(WeatherError):
        w.forecast(123, 0)
    with pytest.raises(WeatherError):
        w.forecast("x", None)


def test_search_compacts_results():
    def fetch(url):
        assert "name=San+Sebasti%C3%A1n" in url and "language=es" in url
        return {"results": [
            {"name": "San Sebastián", "admin1": "País Vasco", "country": "España", "latitude": 43.31283, "longitude": -1.97499},
            {"name": "Broken", "latitude": None, "longitude": 3},
        ]}  # fmt: skip

    w = Weather(fetch=fetch)
    r = w.search("  San   Sebastián ", "es")
    assert r == {"q": "San Sebastián", "results": [
        {"name": "San Sebastián", "region": "País Vasco, España", "lat": 43.3128, "lon": -1.975}]}  # fmt: skip
    assert w.search("a")["results"] == []  # too short: no request


def test_network_failure_is_a_weather_error():
    def fetch(url):
        raise OSError("offline")

    with pytest.raises(WeatherError):
        Weather(fetch=fetch).search("Madrid")


def test_local_clock_has_epoch_and_offset():
    c = local_clock(NOW)
    assert c["epoch"] == int(NOW) and -14 * 60 <= c["utcOffsetMin"] <= 14 * 60


def test_server_answers_weather_messages():
    def fetch(url):
        return forecast_json() if "forecast" in url else {"results": [{"name": "Madrid", "country": "España", "latitude": 40.4, "longitude": -3.7}]}

    async def main():
        srv = AgentServer(demo_provider(), hosts=["127.0.0.1"], port=0, device_id="t", interval_s=0.5,
                          weather=Weather(fetch=fetch))  # fmt: skip
        stop = asyncio.Event()
        task = asyncio.create_task(srv.run(stop))
        await srv.wait_ready()
        try:
            async with connect(f"ws://127.0.0.1:{srv.bound[0][1]}") as ws:
                await ws.send(json.dumps({"v": 1, "type": "hello", "payload": {"clientId": "d", "fw": "t", "protocol": 1}}))

                async def wait(kind):
                    for _ in range(20):
                        m = json.loads(await ws.recv())
                        if m["type"] == kind:
                            return m
                    raise AssertionError(kind)

                snap = await wait(P.T_SNAPSHOT)
                assert "epoch" in snap["payload"]["clock"]
                await ws.send(json.dumps({"v": 1, "type": P.T_WEATHER_SEARCH, "payload": {"q": "Madrid", "lang": "es"}}))
                res = await wait(P.T_WEATHER_RESULTS)
                assert res["payload"]["results"][0]["name"] == "Madrid"
                await asyncio.sleep(1.05)  # one weather message per second per deck
                await ws.send(json.dumps({"v": 1, "type": P.T_WEATHER_REQUEST, "payload": {"lat": 40.4, "lon": -3.7}}))
                data = await wait(P.T_WEATHER_DATA)
                assert data["payload"]["now"]["code"] == 2
        finally:
            stop.set()
            await task

    asyncio.run(asyncio.wait_for(main(), timeout=20))
