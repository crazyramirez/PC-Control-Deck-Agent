"""Crypto market data (Binance / CoinGecko / alternative.me) with a canned upstream: no network."""

import asyncio
import json
import math
import urllib.error
import urllib.parse
from pathlib import Path

import pytest
from websockets.asyncio.client import connect

from pcdeck_agent import protocol as P
from pcdeck_agent.crypto import (
    GECKO_BACKOFF_S,
    Crypto,
    CryptoError,
    aggregate,
    normalise_spark,
    parse_coins,
    pct,
    sig,
)
from pcdeck_agent.hardware.demo_frames import demo_provider
from pcdeck_agent.security.credentials import CredentialStore
from pcdeck_agent.security.pairing import PairingManager
from pcdeck_agent.server import AgentServer

FIX = Path(__file__).resolve().parents[1] / "shared" / "fixtures"
T0 = 1794479100.0
KLINE_T0_MS = 1_700_000_000_000

GECKO = {
    "bitcoin": {"market_cap": 1.6e12, "market_cap_rank": 1, "ath": 126000.0},
    "ethereum": {"market_cap": 3.6e11, "market_cap_rank": 2, "ath": 4950.0},
}


def http_error(url, code):
    return urllib.error.HTTPError(url, code, "canned", None, None)


class Upstream:
    """Fake fetch: routes by host/path like the real APIs, records every URL."""

    def __init__(self):
        self.calls = []
        self.tickers = {"BTCUSDT": 80000.0, "ETHUSDT": 3000.0, "SOLUSDT": 150.0}
        self.fx = {"EURUSDT": 1.25, "USDTBRL": 5.4, "USDTMXN": 18.5, "USDTARS": 1400.0, "USDTCOP": 4000.0}
        self.gecko = dict(GECKO)
        self.binance_error = None  # HTTP status, or "net" for a network failure
        self.gecko_status = None

    def count(self, path):
        return sum(urllib.parse.urlsplit(u).path.endswith(path) for u in self.calls)

    def batches(self):
        return [json.loads(self.query(u)["symbols"]) for u in self.calls if "symbols=" in u]

    @staticmethod
    def query(url):
        return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}

    def ticker(self, s):
        p = self.tickers[s]
        return {"symbol": s, "lastPrice": f"{p:.8f}", "priceChangePercent": "-1.234", "priceChange": f"{-p * 0.01234:.8f}",
                "highPrice": f"{p * 1.02:.8f}", "lowPrice": f"{p * 0.98:.8f}", "quoteVolume": "29100000000.12345678"}  # fmt: skip

    def klines(self, symbol, limit):
        p = self.tickers[symbol]
        rows = []
        for i in range(limit):
            c = p * (1 + i / 1000)
            rows.append([KLINE_T0_MS + i * 60_000, f"{c * 0.999:.8f}", f"{c * 1.01:.8f}", f"{c * 0.99:.8f}", f"{c:.8f}",
                         "12.5", KLINE_T0_MS + i * 60_000 + 59_999, f"{1000.0 + i:.4f}", 10, "1", "1", "0"])  # fmt: skip
        return rows

    def __call__(self, url):
        self.calls.append(url)
        u = urllib.parse.urlsplit(url)
        q = self.query(url)
        if u.netloc == "data-api.binance.vision":
            if self.binance_error == "net":
                raise urllib.error.URLError("offline")
            if self.binance_error:
                raise http_error(url, self.binance_error)
            if u.path == "/api/v3/ticker/price":
                if q["symbol"] not in self.fx:
                    raise http_error(url, 400)
                return {"symbol": q["symbol"], "price": str(self.fx[q["symbol"]])}
            if u.path == "/api/v3/ticker/24hr":
                assert q["type"] == "FULL"
                syms = json.loads(q["symbols"]) if "symbols" in q else [q["symbol"]]
                if any(s not in self.tickers for s in syms):
                    raise http_error(url, 400)  # Binance rejects the whole batch
                rows = [self.ticker(s) for s in syms]
                return rows if "symbols" in q else rows[0]
            if u.path == "/api/v3/klines":
                if q["symbol"] not in self.tickers:
                    raise http_error(url, 400)
                return self.klines(q["symbol"], int(q["limit"]))
        if u.netloc == "api.coingecko.com":
            if self.gecko_status:
                raise http_error(url, self.gecko_status)
            if u.path == "/api/v3/coins/markets":
                assert q["vs_currency"] == "usd" and q["per_page"] == "50"
                return [{"id": i, **self.gecko[i]} for i in q["ids"].split(",") if i in self.gecko]
            if u.path == "/api/v3/global":
                return {"data": {"total_market_cap": {"usd": 3.0e12, "eur": 1}, "market_cap_change_percentage_24h_usd": 1.2367,
                                 "market_cap_percentage": {"btc": 57.3456, "eth": 12.0}}}  # fmt: skip
        if url == "https://api.alternative.me/fng/?limit=1":
            return {"name": "Fear and Greed Index", "data": [{"value": "63", "value_classification": "Greed"}]}
        raise AssertionError(f"unexpected url {url}")


def make(now=None):
    up = Upstream()
    t = [T0] if now is None else now
    return up, Crypto(fetch=up, now=lambda: t[0]), t


BTC_ETH = [{"p": "BTCUSDT", "g": "bitcoin"}, {"p": "ETHUSDT", "g": "ethereum"}]


# ---- pure helpers ---------------------------------------------------------------------------------
def test_sig_rounds_to_significant_digits():
    assert sig(73891.234) == 73891.2
    assert sig(-920.1234) == -920.123
    assert sig(0.0000123456789) == 1.23457e-05
    assert sig(29123456789.5) == 29123500000 and isinstance(sig(29123456789.5), int)
    assert sig(1234.5678, 4) == 1235
    assert sig(0.0) == 0
    for bad in (float("nan"), float("inf"), -math.inf, None, True, "1", 10**400):
        assert sig(bad) is None
    assert pct(-1.23456) == -1.23 and pct(57.3456) == 57.35
    assert pct(float("nan")) is None and pct(None) is None
    assert json.dumps(pct(-0.001)) == "0.0"  # never "-0.0"


def test_normalise_spark():
    assert normalise_spark([1, 2, 3]) == [0, 50, 100]
    assert normalise_spark(["10", "10", "10"]) == [50, 50, 50]  # flat series
    assert normalise_spark([]) == []
    assert normalise_spark([1, "x", float("nan"), 3]) == [0, 100]
    s = normalise_spark(list(range(100)))
    assert len(s) == 48 and s[0] == 0 and s[-1] == 100  # oldest first, last 48 points


def test_aggregate_is_end_aligned():
    rows = [[i, 10 + i, 20 + i, i, 15 + i, 1.0] for i in range(5)]
    out = aggregate(rows, 2)  # k = 3: [0..1] (partial), [2..4] (complete)
    assert out == [[0, 10, 21, 0, 16, 2.0], [2, 12, 24, 2, 19, 3.0]]
    assert aggregate(rows, 5) == rows
    big = [[i, i, i + 1, i - 1, i + 0.5, 1.0] for i in range(1000)]
    agg = aggregate(big, 120)  # k = 9 -> 1 partial + 111 complete buckets
    assert len(agg) == 112 <= 120
    assert agg[0] == [0, 0, 1, -1, 0.5, 1.0]
    assert agg[-1] == [991, 991, 1000, 990, 999.5, 9.0]
    assert len(aggregate(big[:250], 120)) == 84


def test_parse_coins_validates_and_caps():
    coins = [
        {"p": "BTCUSDT", "g": "bitcoin"},
        {"p": "btcusdt"},  # lowercase
        {"p": "BTCEUR"},  # not a USDT pair
        {"p": "ABCDEFGHIJKLMUSDT"},  # base > 12 chars
        {"p": "ETHUSDT", "g": "Ethereum!"},  # bad CoinGecko id -> entry dropped
        {"p": "ETHUSDT", "g": ""},  # empty id: no meta
        "SOLUSDT",
        {"p": 5},
    ] + [{"p": f"C{i}USDT", "g": f"coin-{i}"} for i in range(20)]
    sel = parse_coins(coins)
    assert len(sel) == 12
    assert sel[0] == ("BTCUSDT", "bitcoin") and sel[1] == ("ETHUSDT", "") and sel[2] == ("C0USDT", "coin-0")
    assert parse_coins("BTCUSDT") == [] and parse_coins([{"p": "BTCUSDT\n"}]) == []


# ---- market ---------------------------------------------------------------------------------------
def test_market_eur_uses_the_inverted_rate():
    up, c, _ = make()
    m = c.market(BTC_ETH, "EUR")
    assert m["cur"] == "EUR" and m["fx"] == 0.8  # EURUSDT 1.25 -> 1 / 1.25
    assert m["ts"] == int(T0)
    btc = m["coins"][0]
    assert [x["p"] for x in m["coins"]] == ["BTCUSDT", "ETHUSDT"]
    assert btc["price"] == 64000 and btc["chg"] == -1.23  # % not converted
    assert btc["chgAbs"] == -789.76 and btc["hi"] == 65280 and btc["lo"] == 62720
    assert btc["vol"] == 23280000000
    assert btc["mc"] == 1280000000000 and btc["rank"] == 1 and btc["ath"] == 100800
    assert len(btc["spark"]) == 48 and btc["spark"][0] == 0 and btc["spark"][-1] == 100
    assert all(isinstance(v, int) and 0 <= v <= 100 for v in btc["spark"])
    assert m["global"] == {"mcap": 2400000000000, "mcapChg": 1.24, "btcDom": 57.35, "fng": 63, "fngClass": "Greed"}
    assert any("symbol=EURUSDT" in u for u in up.calls)
    assert any("klines?symbol=BTCUSDT&interval=30m&limit=48" in u for u in up.calls)
    assert any("ids=bitcoin,ethereum" in u for u in up.calls)
    assert up.batches() == [["BTCUSDT", "ETHUSDT"]]
    assert any("symbols=%5B%22BTCUSDT%22%2C%22ETHUSDT%22%5D" in u for u in up.calls)  # URL-encoded JSON array


def test_market_brl_uses_the_direct_rate():
    up, c, _ = make()
    m = c.market(BTC_ETH[:1], "BRL")
    assert m["cur"] == "BRL" and m["fx"] == 5.4
    assert m["coins"][0]["price"] == 432000 and m["coins"][0]["ath"] == 680400


def test_market_falls_back_to_usd_without_a_rate():
    up, c, _ = make()
    del up.fx["USDTMXN"]
    m = c.market(BTC_ETH[:1], "MXN")
    assert m["cur"] == "USD" and m["fx"] == 1 and m["coins"][0]["price"] == 80000
    m = c.market(BTC_ETH[:1], "JPY")  # not supported -> USD, no fx request
    assert m["cur"] == "USD" and up.count("/ticker/price") == 1


def test_market_bad_request():
    up, c, _ = make()
    for coins in ([], None, "BTCUSDT", [{"p": "nope"}], [{"p": "BTCUSDT", "g": "Bad Id"}]):
        with pytest.raises(CryptoError) as e:
            c.market(coins, "EUR")
        assert str(e.value) == "bad_request"
    assert up.calls == []


def test_unknown_symbol_falls_back_per_symbol_with_null_price():
    up, c, t = make()
    coins = [{"p": "BTCUSDT", "g": "bitcoin"}, {"p": "FOOUSDT"}, {"p": "ETHUSDT", "g": "ethereum"}]
    m = c.market(coins, "USD")
    assert [x["p"] for x in m["coins"]] == ["BTCUSDT", "FOOUSDT", "ETHUSDT"]  # request order kept
    foo = m["coins"][1]
    assert foo["price"] is None and foo["chg"] is None and foo["vol"] is None and foo["mc"] is None
    assert foo["spark"] == []
    assert m["coins"][0]["price"] == 80000 and m["coins"][2]["price"] == 3000
    assert up.count("/ticker/24hr") == 4  # rejected batch + one per symbol
    assert not any("klines?symbol=FOOUSDT" in u for u in up.calls)
    # The unknown pair is remembered: the next batch leaves it out and succeeds.
    t[0] += 11
    up.calls.clear()
    m = c.market(coins, "USD")
    assert up.batches() == [["BTCUSDT", "ETHUSDT"]] and up.count("/ticker/24hr") == 1
    assert m["coins"][1]["price"] is None and m["coins"][0]["price"] == 80000


def test_tickers_unavailable_is_an_error():
    for err in ("net", 503):
        up, c, _ = make()
        up.binance_error = err
        with pytest.raises(CryptoError) as e:
            c.market(BTC_ETH, "EUR")
        assert str(e.value) == "unavailable"


def test_coingecko_429_backs_off_for_20_minutes():
    up, c, t = make()
    up.gecko_status = 429
    m = c.market(BTC_ETH, "USD")
    assert m["coins"][0]["price"] == 80000  # Binance data still there
    assert m["coins"][0]["mc"] is None and m["coins"][0]["rank"] is None and m["coins"][0]["ath"] is None
    assert m["global"] == {"mcap": None, "mcapChg": None, "btcDom": None, "fng": 63, "fngClass": "Greed"}
    tried = sum("coingecko" in u for u in up.calls)
    assert 1 <= tried <= 2  # markets and global run concurrently; nothing after the 429
    up.gecko_status = None
    t[0] += 11 * 60  # meta / global cache expired, but still inside the back-off
    c.market(BTC_ETH, "USD")
    assert sum("coingecko" in u for u in up.calls) == tried
    t[0] = T0 + GECKO_BACKOFF_S + 1
    m = c.market(BTC_ETH, "USD")
    assert up.count("/coins/markets") >= 1 and m["coins"][0]["mc"] == 1600000000000
    assert m["global"]["mcap"] == 3000000000000


def test_coingecko_429_keeps_the_last_known_values():
    up, c, t = make()
    c.market(BTC_ETH, "USD")
    up.gecko_status = 429
    t[0] += 11 * 60
    m = c.market(BTC_ETH, "USD")
    assert m["coins"][0]["mc"] == 1600000000000 and m["global"]["btcDom"] == 57.35


def test_global_is_null_when_everything_failed():
    up, c, _ = make()
    up.gecko_status = 503
    orig = up.__call__

    def fetch(url):
        if "alternative.me" in url:
            raise urllib.error.URLError("offline")
        return orig(url)

    c = Crypto(fetch=fetch, now=lambda: T0)
    assert c.market(BTC_ETH, "USD")["global"] is None


def test_cache_ttls():
    up, c, t = make()
    c.market(BTC_ETH, "EUR")
    base = {k: up.count(k) for k in ("/ticker/price", "/ticker/24hr", "/klines", "/coins/markets", "/global", "/fng/")}
    assert base == {"/ticker/price": 1, "/ticker/24hr": 1, "/klines": 2, "/coins/markets": 1, "/global": 1, "/fng/": 1}

    def delta():
        return {k: up.count(k) - v for k, v in base.items() if up.count(k) - v}

    t[0] = T0 + 9
    c.market(BTC_ETH, "EUR")
    assert delta() == {}  # everything cached
    t[0] = T0 + 11
    c.market(BTC_ETH, "EUR")
    assert delta() == {"/ticker/24hr": 1}  # tickers: 10 s
    t[0] = T0 + 61
    c.market(BTC_ETH, "EUR")
    assert delta() == {"/ticker/24hr": 2, "/ticker/price": 1}  # fx: 60 s
    t[0] = T0 + 5 * 60 + 1
    c.market(BTC_ETH, "EUR")
    assert delta() == {"/ticker/24hr": 3, "/ticker/price": 2, "/klines": 2}  # sparks: 5 min
    t[0] = T0 + 10 * 60 + 1
    c.market(BTC_ETH, "EUR")
    d = delta()
    assert d["/coins/markets"] == 1 and d["/global"] == 1 and "/fng/" not in d  # CoinGecko: 10 min
    t[0] = T0 + 30 * 60 + 1
    c.market(BTC_ETH, "EUR")
    assert delta()["/fng/"] == 1  # Fear & Greed: 30 min


def test_ticker_cache_fetches_only_missing_pairs():
    up, c, t = make()
    c.market(BTC_ETH[:1], "USD")
    t[0] += 5
    c.market(BTC_ETH + [{"p": "SOLUSDT"}], "USD")
    assert up.batches() == [["BTCUSDT"], ["ETHUSDT", "SOLUSDT"]]
    t[0] += 6  # BTC expired (11 s), ETH / SOL still fresh (6 s)
    c.market(BTC_ETH + [{"p": "SOLUSDT"}], "USD")
    assert up.batches()[-1] == ["BTCUSDT"]


def test_market_message_for_12_coins_fits():
    up, c, _ = make()
    coins = []
    for i in range(12):
        p = f"LONGNAME{i:02d}USDT"
        up.tickers[p] = 0.0000123456789 if i % 2 else 123456.789012
        up.gecko.setdefault(f"long-coin-id-{i:02d}", {"market_cap": 1.23456789e12, "market_cap_rank": 1000 + i, "ath": 0.000098765})
        coins.append({"p": p, "g": f"long-coin-id-{i:02d}"})
    m = c.market(coins, "COP")
    assert len(m["coins"]) == 12 and all(len(x["spark"]) == 48 for x in m["coins"])
    size = len(P.encode(P.envelope(P.T_CRYPTO_DATA, m, device_id="desktop-i9")).encode("utf-8"))
    assert size < 12 * 1024, size


# ---- candles --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "rng,interval,limit,rows",
    [("1h", "1m", 60, 60), ("24h", "15m", 96, 96), ("7d", "2h", 84, 84), ("1m", "6h", 120, 120),
     ("3m", "1d", 90, 90), ("1y", "3d", 122, 120), ("all", "1w", 1000, 112)],  # fmt: skip
)
def test_candle_ranges(rng, interval, limit, rows):
    up, c, _ = make()
    out = c.candles("BTCUSDT", rng, "USD")
    assert up.calls[-1].endswith(f"/api/v3/klines?symbol=BTCUSDT&interval={interval}&limit={limit}")
    assert out["p"] == "BTCUSDT" and out["r"] == rng and out["cur"] == "USD"
    assert len(out["c"]) == rows <= 120
    assert all(len(r) == 6 and isinstance(r[0], int) for r in out["c"])
    assert out["c"] == sorted(out["c"])  # oldest first
    raw = up.klines("BTCUSDT", limit)
    last = raw[-1]
    assert out["c"][-1][4] == sig(float(last[4]))  # newest close is the last candle's close
    if rng == "1y":
        assert out["c"][0][0] == raw[2][0] // 1000  # the last 120 of 122
    if rng == "all":
        k = 9  # ceil(1000 / 120), aligned to the end: newest bucket = last 9 candles
        tail = raw[-k:]
        assert out["c"][-1][0] == tail[0][0] // 1000
        assert out["c"][-1][1] == sig(float(tail[0][1]))
        assert out["c"][-1][2] == sig(max(float(r[2]) for r in tail))
        assert out["c"][-1][3] == sig(min(float(r[3]) for r in tail))
        assert out["c"][-1][5] == sig(sum(float(r[7]) for r in tail), 4)
        assert out["c"][0][0] == raw[0][0] // 1000  # oldest bucket is the partial one


def test_candles_convert_per_request_from_one_cached_fetch():
    up, c, t = make()
    eur = c.candles("ETHUSDT", "24h", "EUR")
    brl = c.candles("ETHUSDT", "24h", "BRL")
    assert up.count("/klines") == 1
    raw = up.klines("ETHUSDT", 96)[0]
    assert eur["c"][0] == [raw[0] // 1000, sig(float(raw[1]) * 0.8), sig(float(raw[2]) * 0.8), sig(float(raw[3]) * 0.8),
                           sig(float(raw[4]) * 0.8), sig(float(raw[7]) * 0.8, 4)]  # fmt: skip
    assert brl["cur"] == "BRL" and brl["c"][0][4] == sig(float(raw[4]) * 5.4)
    t[0] += 61  # 24h range: 60 s TTL
    c.candles("ETHUSDT", "24h", "EUR")
    assert up.count("/klines") == 2
    c.candles("ETHUSDT", "all", "EUR")
    t[0] += 5 * 3600
    c.candles("ETHUSDT", "all", "EUR")
    assert sum("interval=1w" in u for u in up.calls) == 1  # all: 6 h TTL


def test_candles_errors():
    up, c, _ = make()
    for pair, rng in (("btcusdt", "24h"), ("BTCUSDT", "2h"), (None, "24h"), ("BTCUSDT", ["24h"]), ("BTC", "1h")):
        with pytest.raises(CryptoError) as e:
            c.candles(pair, rng, "EUR")
        assert str(e.value) == "bad_request"
    with pytest.raises(CryptoError) as e:
        c.candles("FOOUSDT", "24h", "EUR")  # Binance does not list it
    assert str(e.value) == "unavailable"
    up.binance_error = 502
    with pytest.raises(CryptoError) as e:
        c.candles("BTCUSDT", "1h", "EUR")
    assert str(e.value) == "unavailable"


def test_candles_message_for_120_rows_fits():
    up, c, _ = make()
    up.tickers["PEPEUSDT"] = 0.0000123456789
    for pair in ("PEPEUSDT", "BTCUSDT"):
        out = c.candles(pair, "1m", "COP")
        assert len(out["c"]) == 120
        size = len(P.encode(P.envelope(P.T_CRYPTO_CANDLES, out, device_id="desktop-i9")).encode("utf-8"))
        assert size < 12 * 1024, size


# ---- fixtures / protocol --------------------------------------------------------------------------
def test_fixtures_match_the_agent_output():
    up, c, _ = make()
    data = P.decode((FIX / "crypto_data.json").read_text(encoding="utf-8"))
    assert data["type"] == P.T_CRYPTO_DATA
    ours = c.market(BTC_ETH, "EUR")
    ref = data["payload"]
    assert set(ours) == set(ref) and set(ours["global"]) == set(ref["global"])
    assert len(ref["coins"]) == 2 and all(set(x) == set(ours["coins"][0]) for x in ref["coins"])
    assert all(len(x["spark"]) <= 48 for x in ref["coins"])
    candles = P.decode((FIX / "crypto_candles.json").read_text(encoding="utf-8"))
    assert candles["type"] == P.T_CRYPTO_CANDLES
    ours = c.candles("BTCUSDT", "24h", "EUR")
    assert set(ours) == set(candles["payload"])
    rows = candles["payload"]["c"]
    assert len(rows) == 5 and all(len(r) == 6 and isinstance(r[0], int) for r in rows)
    assert rows == sorted(rows)


def test_decode_accepts_the_deck_requests():
    for t, payload in ((P.T_CRYPTO_REQUEST, {"coins": BTC_ETH, "cur": "EUR"}), (P.T_CRYPTO_CHART, {"p": "BTCUSDT", "r": "24h", "cur": "EUR"})):
        assert P.decode(json.dumps({"v": 1, "type": t, "payload": payload}))["type"] == t


# ---- server ---------------------------------------------------------------------------------------
def test_server_serves_crypto_to_an_authenticated_deck_only(tmp_path):
    up = Upstream()

    async def main():
        creds = CredentialStore(tmp_path / "devices.dat", encrypt=False)
        token = creds.add("deck-c", "Mesa")
        srv = AgentServer(demo_provider(), hosts=["127.0.0.1"], port=0, device_id="pc", interval_s=0.5,
                          credentials=creds, pairing=PairingManager(), crypto=Crypto(fetch=up))  # fmt: skip
        stop = asyncio.Event()
        task = asyncio.create_task(srv.run(stop))
        await srv.wait_ready()
        try:
            async with connect(f"ws://127.0.0.1:{srv.bound[0][1]}") as ws:

                async def send(t, payload):
                    await ws.send(json.dumps({"v": 1, "type": t, "payload": payload}))

                async def collect(seconds):
                    out = []
                    try:
                        while True:
                            out.append(json.loads(await asyncio.wait_for(ws.recv(), seconds)))
                    except asyncio.TimeoutError:
                        return out

                async def wait(kind):
                    for _ in range(30):
                        m = json.loads(await asyncio.wait_for(ws.recv(), 5))
                        if m["type"] == kind:
                            return m
                    raise AssertionError(kind)

                await send(P.T_HELLO, {"clientId": "deck-c", "fw": "t", "protocol": 1})
                await wait(P.T_CAPABILITIES)
                request = {"coins": BTC_ETH, "cur": "EUR"}
                await send(P.T_CRYPTO_REQUEST, request)  # not authenticated yet: ignored
                assert all(m["type"] != P.T_CRYPTO_DATA for m in await collect(0.5))
                assert up.calls == []

                await send(P.T_AUTH_REQUEST, {"token": token})
                assert (await wait(P.T_AUTH_RESULT))["payload"]["ok"] is True
                await send(P.T_CRYPTO_REQUEST, request)
                data = await wait(P.T_CRYPTO_DATA)
                assert data["deviceId"] == "pc"
                assert data["payload"]["cur"] == "EUR" and data["payload"]["coins"][0]["price"] == 64000
                await send(P.T_CRYPTO_REQUEST, request)  # < 2 s after the last one: dropped
                assert all(m["type"] != P.T_CRYPTO_DATA for m in await collect(0.5))

                await send(P.T_CRYPTO_CHART, {"p": "BTCUSDT", "r": "24h", "cur": "EUR"})
                await send(P.T_CRYPTO_CHART, {"p": "BTCUSDT", "r": "7d", "cur": "EUR"})  # < 0.3 s: dropped
                candles = await wait(P.T_CRYPTO_CANDLES)
                assert candles["payload"]["r"] == "24h" and len(candles["payload"]["c"]) == 96
                assert all(m["type"] != P.T_CRYPTO_CANDLES for m in await collect(0.4))
                await send(P.T_CRYPTO_CHART, {"p": "nope", "r": "24hours", "cur": "EUR"})
                bad = await wait(P.T_CRYPTO_CANDLES)
                assert bad["payload"] == {"p": "nope", "r": "24ho", "error": "bad_request"}

                await asyncio.sleep(1.2)  # + the 0.9 s of collect() above: > 2 s since the last market
                await send(P.T_CRYPTO_REQUEST, {"coins": [{"p": "x"}], "cur": "ZZZ"})
                err = await wait(P.T_CRYPTO_DATA)
                assert err["payload"] == {"cur": "USD", "error": "bad_request"}

                up.binance_error = 503
                await asyncio.sleep(0.35)
                await send(P.T_CRYPTO_CHART, {"p": "SOLUSDT", "r": "1h", "cur": "BRL"})
                down = await wait(P.T_CRYPTO_CANDLES)
                assert down["payload"] == {"p": "SOLUSDT", "r": "1h", "cur": "BRL", "error": "unavailable"}
                # The session survived all of the above.
                await wait(P.T_SNAPSHOT)
        finally:
            stop.set()
            await task

    asyncio.run(asyncio.wait_for(main(), timeout=30))
