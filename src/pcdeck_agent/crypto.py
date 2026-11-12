"""Crypto market data for the deck's "Crypto" tab, from public endpoints (no API key, no account).

- Prices, 24 h stats, sparklines and charts: Binance public market data (data-api.binance.vision).
- Market cap, rank, all-time high and the global market: CoinGecko (api.coingecko.com).
- Fear & Greed index: alternative.me.

The only things that leave the PC are the coin pairs / CoinGecko ids the deck asks for and the
display currency. Everything is cached (see the *_TTL_S constants) so several decks / refreshes
don't multiply requests; CoinGecko answers 429 quickly without a key, so a 429 pauses it for 20 min.

Payloads (docs/PROTOCOL.md):
  market  -> {"cur", "fx", "coins": [{p, price, chg, chgAbs, hi, lo, vol, mc, rank, ath, spark}], "global", "ts"}
  candles -> {"p", "r", "cur", "c": [[t, o, h, l, c, v], ...]}
Money values are USD(T) upstream, converted with ``fx`` and rounded to 6 significant digits.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, NamedTuple

log = logging.getLogger(__name__)

BINANCE_URL = "https://data-api.binance.vision"
COINGECKO_URL = "https://api.coingecko.com/api/v3"
FNG_URL = "https://api.alternative.me/fng/?limit=1"
USER_AGENT = "PCControlDeck-Agent"
TIMEOUT_S = 6.0
MAX_RESPONSE = 1024 * 1024

MAX_COINS = 12
MAX_ROWS = 120  # candles per chart
SPARK_POINTS = 48  # 24 h of 30 min closes
TICKER_TTL_S = 10
UNKNOWN_TTL_S = 10 * 60  # a pair Binance does not list: don't retry the batch / fallback every 10 s
SPARK_TTL_S = 5 * 60
META_TTL_S = 10 * 60
GLOBAL_TTL_S = 10 * 60
FNG_TTL_S = 30 * 60
FX_TTL_S = 60
GECKO_BACKOFF_S = 20 * 60  # after an HTTP 429 from CoinGecko
STALE_MAX_S = 60 * 60  # a refresh failed: keep serving the last good value up to this age

CURRENCIES = ("USD", "EUR", "MXN", "ARS", "COP", "BRL")
# currency -> (Binance symbol, invert): EURUSDT quotes USD per EUR (fx = 1/p), USDTxxx quotes xxx per USD.
FX_SYMBOLS = {"EUR": ("EURUSDT", True), "MXN": ("USDTMXN", False), "ARS": ("USDTARS", False),
              "COP": ("USDTCOP", False), "BRL": ("USDTBRL", False)}  # fmt: skip
# range -> (kline interval, limit, cache TTL s)
RANGES: dict[str, tuple[str, int, int]] = {
    "1h": ("1m", 60, 30),
    "24h": ("15m", 96, 60),
    "7d": ("2h", 84, 5 * 60),
    "1m": ("6h", 120, 15 * 60),
    "3m": ("1d", 90, 30 * 60),
    "1y": ("3d", 122, 60 * 60),
    "all": ("1w", 1000, 6 * 60 * 60),
}

PAIR_RE = re.compile(r"[A-Z0-9]{1,12}USDT")
GECKO_ID_RE = re.compile(r"[a-z0-9-]{1,48}")

# url -> parsed JSON (injectable for tests). HTTP errors raise urllib.error.HTTPError, so callers can
# tell a 400 (unknown Binance symbol) or a 429 (CoinGecko rate limit) from a network failure.
Fetch = Callable[[str], Any]


class CryptoError(Exception):
    """Total failure; str(e) is the deck-facing code ("bad_request" / "unavailable")."""


class Coin(NamedTuple):
    p: str  # Binance pair, e.g. BTCUSDT
    g: str  # CoinGecko id ("" = no market cap / rank / ATH)


def _http_json(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:  # noqa: S310 (fixed https hosts)
        return json.loads(r.read(MAX_RESPONSE).decode("utf-8"))


def http_status(e: BaseException) -> int | None:
    return e.code if isinstance(e, urllib.error.HTTPError) else None


# ---- pure helpers ---------------------------------------------------------------------------------
def _f(v: Any) -> float | None:
    """Number or numeric string (Binance sends strings) -> finite float, else None."""
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        return None
    try:
        x = float(v)
    except (ValueError, OverflowError):
        return None
    return x if math.isfinite(x) else None


def sig(v: Any, n: int = 6) -> float | int | None:
    """Round to n significant digits; integral results become ints (compact JSON); non-finite -> None."""
    x = _f(v) if not isinstance(v, str) else None
    if x is None:
        return None
    r = float(f"{x:.{n}g}")
    return int(r) if r.is_integer() and abs(r) < 1e15 else r


def pct(v: Any) -> float | None:
    """Percentage rounded to 0.01 (non-finite / missing -> None)."""
    x = v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    return round(float(x), 2) + 0.0 if x is not None and math.isfinite(x) else None  # + 0.0: no "-0.0"


def _money(v: float | None, fx: float, n: int = 6) -> float | int | None:
    return None if v is None else sig(v * fx, n)


def normalise_spark(closes: list[Any], points: int = SPARK_POINTS) -> list[int]:
    """Closes (oldest first) -> ints 0..100 (min -> 0, max -> 100, flat -> 50), at most `points`."""
    vals = [x for x in (_f(c) for c in closes) if x is not None][-points:]
    if not vals:
        return []
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        return [50] * len(vals)
    return [max(0, min(100, int(round((x - lo) / (hi - lo) * 100)))) for x in vals]


def aggregate(rows: list[list[float]], max_rows: int = MAX_ROWS) -> list[list[float]]:
    """Merge [t, o, h, l, c, v] rows into <= max_rows buckets of k = ceil(n / max_rows) rows.

    Buckets are aligned to the END: the newest bucket is complete, the oldest may be partial.
    """
    n = len(rows)
    if n <= max_rows:
        return [list(r) for r in rows]
    k = -(-n // max_rows)
    out = []
    start = n % k
    bounds = ([(0, start)] if start else []) + [(i, i + k) for i in range(start, n, k)]
    for a, b in bounds:
        g = rows[a:b]
        out.append([g[0][0], g[0][1], max(r[2] for r in g), min(r[3] for r in g), g[-1][4], sum(r[5] for r in g)])
    return out


def parse_klines(data: Any) -> list[list[float]]:
    """Binance klines -> [[t_s, o, h, l, c, quoteVol], ...] (USD), invalid rows skipped."""
    out: list[list[float]] = []
    if not isinstance(data, list):
        return out
    for r in data:
        if not isinstance(r, list) or len(r) < 8:
            continue
        t = _f(r[0])
        vals = [_f(r[i]) for i in (1, 2, 3, 4, 7)]
        if t is None or any(v is None for v in vals):
            continue
        out.append([int(t // 1000), *vals])  # type: ignore[list-item]
    return out


def norm_cur(cur: Any) -> str:
    return cur if isinstance(cur, str) and cur in CURRENCIES else "USD"


def parse_coins(coins: Any) -> list[Coin]:
    """Deck's coin list -> valid entries (bad ones dropped), first MAX_COINS kept."""
    out: list[Coin] = []
    if not isinstance(coins, list):
        return out
    for c in coins:
        if not isinstance(c, dict):
            continue
        p, g = c.get("p"), c.get("g")
        if not isinstance(p, str) or not PAIR_RE.fullmatch(p):
            continue
        if g is None or g == "":
            g = ""
        elif not isinstance(g, str) or not GECKO_ID_RE.fullmatch(g):
            continue
        out.append(Coin(p, g))
        if len(out) >= MAX_COINS:
            break
    return out


def _ticker(row: Any) -> dict[str, float | None] | None:
    """Binance 24 h ticker -> USD values (None: not a ticker, i.e. unknown symbol)."""
    if not isinstance(row, dict):
        return None
    return {
        "price": _f(row.get("lastPrice")),
        "chg": _f(row.get("priceChangePercent")),
        "chgAbs": _f(row.get("priceChange")),
        "hi": _f(row.get("highPrice")),
        "lo": _f(row.get("lowPrice")),
        "vol": _f(row.get("quoteVolume")),
    }


def _meta(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    rank = row.get("market_cap_rank")
    return {
        "mc": _f(row.get("market_cap")),
        "rank": rank if isinstance(rank, int) and not isinstance(rank, bool) and rank > 0 else None,
        "ath": _f(row.get("ath")),
    }


def _ascii(s: Any, limit: int) -> str | None:
    if not isinstance(s, str):
        return None
    return "".join(c for c in s if 32 <= ord(c) < 127).strip()[:limit] or None


# ---- caches ---------------------------------------------------------------------------------------
_MISS = object()


class _TTLCache:
    """Thread-safe key -> value with a per-entry TTL; expired entries stay for stale fallbacks."""

    def __init__(self) -> None:
        self._d: dict[Any, tuple[float, float, Any]] = {}
        self._lock = threading.Lock()

    def put(self, key: Any, value: Any, now: float, ttl: float) -> None:
        with self._lock:
            self._d[key] = (now, ttl, value)

    def get(self, key: Any, now: float, max_age: float | None = None) -> Any:
        """Fresh value (or one younger than max_age when given), else _MISS."""
        with self._lock:
            hit = self._d.get(key)
        if hit is None:
            return _MISS
        t, ttl, value = hit
        return value if now - t < (ttl if max_age is None else max_age) else _MISS


def _run_all(jobs: list[Callable[[], None]]) -> None:
    """Run independent upstream refreshes concurrently (each job handles its own errors)."""
    if len(jobs) <= 1:
        for j in jobs:
            j()
        return
    with ThreadPoolExecutor(max_workers=min(6, len(jobs)), thread_name_prefix="crypto") as ex:
        list(ex.map(lambda j: j(), jobs))


class Crypto:
    def __init__(self, fetch: Fetch | None = None, now: Callable[[], float] = time.time) -> None:
        self._fetch = fetch or _http_json
        self._now = now
        self._lock = threading.Lock()
        self._key_locks: dict[Any, threading.Lock] = {}
        self._tickers = _TTLCache()  # pair -> USD ticker dict, or None = unknown symbol
        self._sparks = _TTLCache()  # pair -> [0..100]
        self._metas = _TTLCache()  # coingecko id -> {mc, rank, ath} or None
        self._global = _TTLCache()  # "g" -> {mcap, mcapChg, btcDom}
        self._fng = _TTLCache()  # "f" -> {fng, fngClass}
        self._fx = _TTLCache()  # currency -> fx
        self._charts = _TTLCache()  # (pair, range) -> USD rows
        self._gecko_until = 0.0

    def _key_lock(self, key: Any) -> threading.Lock:
        """One lock per upstream resource: concurrent identical requests wait and hit the cache."""
        with self._lock:
            return self._key_locks.setdefault(key, threading.Lock())

    def _get(self, base: str, path: str, params: dict[str, Any] | None = None, safe: str = "") -> Any:
        url = base + path + ("?" + urllib.parse.urlencode(params, safe=safe) if params else "")
        return self._fetch(url)

    # ---- fx -------------------------------------------------------------------------------------
    def fx(self, cur: Any) -> tuple[str, float]:
        """(currency, USD -> currency multiplier); USD when the rate cannot be obtained."""
        cur = norm_cur(cur)
        if cur == "USD":
            return "USD", 1.0
        with self._key_lock(("fx", cur)):
            now = self._now()
            v = self._fx.get(cur, now)
            if v is not _MISS:
                return cur, v
            sym, invert = FX_SYMBOLS[cur]
            try:
                p = _f((self._get(BINANCE_URL, "/api/v3/ticker/price", {"symbol": sym}) or {}).get("price"))
                if p is None or p <= 0:
                    raise ValueError("bad price")
                v = 1.0 / p if invert else p
                self._fx.put(cur, v, now, FX_TTL_S)
                return cur, v
            except Exception as e:
                log.debug("fx %s unavailable: %s", sym, type(e).__name__)
                v = self._fx.get(cur, now, STALE_MAX_S)
                return (cur, v) if v is not _MISS else ("USD", 1.0)

    # ---- tickers --------------------------------------------------------------------------------
    def _fetch_tickers(self, pairs: list[str]) -> dict[str, dict | None]:
        """Batch 24 h tickers; an unknown symbol makes Binance reject the batch (400): then per symbol."""
        try:
            data = self._get(BINANCE_URL, "/api/v3/ticker/24hr", {"symbols": json.dumps(pairs, separators=(",", ":")), "type": "FULL"})
        except Exception as e:
            if http_status(e) != 400:
                log.debug("tickers unavailable: %s", type(e).__name__)
                raise CryptoError("unavailable") from None
            if len(pairs) == 1:
                return {pairs[0]: None}
            return self._fetch_tickers_each(pairs)
        if not isinstance(data, list):
            raise CryptoError("unavailable")
        rows = {r.get("symbol"): r for r in data if isinstance(r, dict)}
        return {p: _ticker(rows.get(p)) for p in pairs}

    def _fetch_tickers_each(self, pairs: list[str]) -> dict[str, dict | None]:
        out: dict[str, dict | None] = {}
        failed: list[str] = []

        def one(p: str) -> None:
            try:
                out[p] = _ticker(self._get(BINANCE_URL, "/api/v3/ticker/24hr", {"symbol": p, "type": "FULL"}))
            except Exception as e:
                if http_status(e) == 400:
                    out[p] = None  # Binance does not list it
                else:
                    failed.append(p)  # transient: price null this time, not cached

        _run_all([lambda p=p: one(p) for p in pairs])
        if len(failed) == len(pairs):
            raise CryptoError("unavailable")
        return out

    def _tickers_for(self, pairs: list[str]) -> dict[str, dict | None]:
        now = self._now()
        out: dict[str, dict | None] = {}
        missing = []
        for p in pairs:
            v = self._tickers.get(p, now)
            if v is _MISS:
                missing.append(p)
            else:
                out[p] = v
        if missing:
            got = self._fetch_tickers(missing)
            for p, v in got.items():
                self._tickers.put(p, v, now, TICKER_TTL_S if v is not None else UNKNOWN_TTL_S)
            out.update(got)
        return out

    # ---- refresh jobs (errors swallowed: the field becomes null) ----------------------------------
    def _refresh_spark(self, pair: str) -> None:
        try:
            rows = self._get(BINANCE_URL, "/api/v3/klines", {"symbol": pair, "interval": "30m", "limit": SPARK_POINTS})
            closes = [r[4] for r in rows if isinstance(r, list) and len(r) > 4] if isinstance(rows, list) else []
        except Exception as e:
            log.debug("spark %s unavailable: %s", pair, type(e).__name__)
            return
        self._sparks.put(pair, normalise_spark(closes), self._now(), SPARK_TTL_S)

    def _gecko_blocked(self) -> bool:
        return self._now() < self._gecko_until

    def _gecko_failed(self, what: str, e: BaseException) -> None:
        if http_status(e) == 429:
            self._gecko_until = self._now() + GECKO_BACKOFF_S
            log.info("CoinGecko rate limit (429): pausing it for %d min", GECKO_BACKOFF_S // 60)
        else:
            log.debug("CoinGecko %s unavailable: %s", what, type(e).__name__)

    def _refresh_meta(self, ids: list[str]) -> None:
        if self._gecko_blocked():
            return
        try:
            data = self._get(COINGECKO_URL, "/coins/markets", {"vs_currency": "usd", "ids": ",".join(ids), "per_page": 50}, safe=",")
        except Exception as e:
            self._gecko_failed("markets", e)
            return
        if not isinstance(data, list):
            return
        rows = {r.get("id"): r for r in data if isinstance(r, dict)}
        now = self._now()
        for i in ids:
            self._metas.put(i, _meta(rows.get(i)), now, META_TTL_S)

    def _refresh_global(self) -> None:
        if self._gecko_blocked():
            return
        try:
            data = (self._get(COINGECKO_URL, "/global") or {}).get("data")
        except Exception as e:
            self._gecko_failed("global", e)
            return
        if not isinstance(data, dict):
            return
        mcap = data.get("total_market_cap") if isinstance(data.get("total_market_cap"), dict) else {}
        dom = data.get("market_cap_percentage") if isinstance(data.get("market_cap_percentage"), dict) else {}
        val = {"mcap": _f(mcap.get("usd")), "mcapChg": _f(data.get("market_cap_change_percentage_24h_usd")), "btcDom": _f(dom.get("btc"))}
        self._global.put("g", val, self._now(), GLOBAL_TTL_S)

    def _refresh_fng(self) -> None:
        try:
            rows = (self._fetch(FNG_URL) or {}).get("data")
            row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
        except Exception as e:
            log.debug("fear & greed unavailable: %s", type(e).__name__)
            return
        v = _f(row.get("value"))
        val = {"fng": int(v) if v is not None and 0 <= v <= 100 else None, "fngClass": _ascii(row.get("value_classification"), 19)}
        if val["fng"] is not None or val["fngClass"] is not None:
            self._fng.put("f", val, self._now(), FNG_TTL_S)

    # ---- public API -----------------------------------------------------------------------------
    def market(self, coins: Any, cur: Any) -> dict[str, Any]:
        """crypto.data payload for the deck's coin list. Raises CryptoError on total failure."""
        sel = parse_coins(coins)
        if not sel:
            raise CryptoError("bad_request")
        with self._key_lock("market"):
            cur, fx = self.fx(cur)
            pairs = list(dict.fromkeys(c.p for c in sel))
            tickers = self._tickers_for(pairs)
            now = self._now()
            jobs: list[Callable[[], None]] = [
                (lambda p=p: self._refresh_spark(p)) for p in pairs if tickers.get(p) is not None and self._sparks.get(p, now) is _MISS
            ]
            ids = [g for g in dict.fromkeys(c.g for c in sel) if g and self._metas.get(g, now) is _MISS]
            if ids:
                jobs.append(lambda: self._refresh_meta(ids))
            if self._global.get("g", now) is _MISS:
                jobs.append(self._refresh_global)
            if self._fng.get("f", now) is _MISS:
                jobs.append(self._refresh_fng)
            _run_all(jobs)
            now = self._now()
            out = []
            for c in sel:
                t = tickers.get(c.p) or {}
                m = self._metas.get(c.g, now, STALE_MAX_S) if c.g else None
                m = m if isinstance(m, dict) else {}
                spark = self._sparks.get(c.p, now, STALE_MAX_S) if c.p in tickers and tickers[c.p] is not None else []
                out.append({
                    "p": c.p,
                    "price": _money(t.get("price"), fx),
                    "chg": pct(t.get("chg")),
                    "chgAbs": _money(t.get("chgAbs"), fx),
                    "hi": _money(t.get("hi"), fx),
                    "lo": _money(t.get("lo"), fx),
                    "vol": _money(t.get("vol"), fx),
                    "mc": _money(m.get("mc"), fx),
                    "rank": m.get("rank"),
                    "ath": _money(m.get("ath"), fx),
                    "spark": spark if isinstance(spark, list) else [],
                })  # fmt: skip
            g = self._global.get("g", now, STALE_MAX_S)
            g = g if isinstance(g, dict) else {}
            f = self._fng.get("f", now, STALE_MAX_S)
            f = f if isinstance(f, dict) else {}
            glob: dict[str, Any] | None = {
                "mcap": _money(g.get("mcap"), fx),
                "mcapChg": pct(g.get("mcapChg")),
                "btcDom": pct(g.get("btcDom")),
                "fng": f.get("fng"),
                "fngClass": f.get("fngClass"),
            }
            if all(v is None for v in glob.values()):
                glob = None
        return {"cur": cur, "fx": sig(fx), "coins": out, "global": glob, "ts": int(now)}

    def candles(self, pair: Any, rng: Any, cur: Any) -> dict[str, Any]:
        """crypto.candles payload: <= 120 [t, o, h, l, c, v] rows, oldest first. Raises CryptoError."""
        if not isinstance(pair, str) or not PAIR_RE.fullmatch(pair) or not isinstance(rng, str) or rng not in RANGES:
            raise CryptoError("bad_request")
        interval, limit, ttl = RANGES[rng]
        with self._key_lock(("chart", pair, rng)):
            now = self._now()
            rows = self._charts.get((pair, rng), now)
            if rows is _MISS:
                try:
                    data = self._get(BINANCE_URL, "/api/v3/klines", {"symbol": pair, "interval": interval, "limit": limit})
                except Exception as e:
                    log.debug("klines %s %s unavailable: %s", pair, rng, type(e).__name__)
                    raise CryptoError("unavailable") from None
                rows = parse_klines(data)
                if not rows:
                    raise CryptoError("unavailable")
                rows = aggregate(rows, MAX_ROWS) if rng == "all" else rows[-MAX_ROWS:]
                self._charts.put((pair, rng), rows, now, ttl)
        cur, fx = self.fx(cur)
        c = [[r[0], sig(r[1] * fx), sig(r[2] * fx), sig(r[3] * fx), sig(r[4] * fx), sig(r[5] * fx, 4)] for r in rows]
        return {"p": pair, "r": rng, "cur": cur, "c": c}
