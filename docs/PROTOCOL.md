# DECKY protocol v1

Between **DECKY Agent** (the desktop app, "the agent" below) and a DECKY deck. The message names
are unchanged; the identifiers on the wire (the mDNS service `_pcdeck._tcp`, the `PCDA` art frames)
keep the project's earlier name, PC Control Deck.

WebSocket over TCP inside the LAN. The **agent listens** (default port **8765**), the **deck connects**.
Text frames, UTF-8 JSON, one message per frame. Fixtures: [`shared/fixtures/`](../shared/fixtures/).

> **Security notice:** plain `ws://`, no TLS. Telemetry travels **unencrypted** on the LAN.
> Access requires pairing (one-time code) and per-device token authentication. Only the
> allow-listed actions below exist. See SECURITY.md and ADR 0003.

## Envelope

```json
{ "v": 1, "type": "telemetry.snapshot", "requestId": "…", "deviceId": "…", "ts": "2026-11-11T21:05:00Z", "payload": { } }
```

| Field | Rule |
|---|---|
| `v` | integer, protocol major. Receivers reject other majors with `error.unsupported_version` and close. |
| `type` | one of the types below; unknown → `error.unknown_type` |
| `requestId` | required on `command.request` / `command.result` (correlation) |
| `deviceId` | agent identity (short host name or configured id) on agent → deck messages |
| `ts` | ISO-8601 UTC, agent → deck |
| `payload` | object, required (may be `{}`) |

Limits: max frame **16 KiB** (larger → close 1009). Malformed JSON / wrong field types → `error.malformed`.
Rate limit: > 20 inbound messages/s from one client → close 1008.

## Messages

| Type | Dir | Payload |
|---|---|---|
| `hello` | deck → agent | `{clientId, fw, protocol, caps[]}` — must be the first message, within 5 s |
| `telemetry.capabilities` | agent → deck | `{agent, agentId, agentName, provider, actions[], notes[], authRequired, authenticated}` — after `hello` (minimal while unauthenticated) and again after auth/pair |
| `pair.request` | deck → agent | `{code: "6 digits", name}` — one-time code shown in the PC app |
| `pair.result` | agent → deck | `{ok, token, agentId, agentName}` or `{ok:false, error:"invalid_code"}` |
| `auth.request` | deck → agent | `{token}` — sent right after `hello` by a paired deck |
| `auth.result` | agent → deck | `{ok}` or `{ok:false, error:"unauthorized"}` |
| `telemetry.snapshot` | agent → deck | see below, ~1 Hz |
| `heartbeat` | both | `{}` every 5 s |
| `error` | both | `{code, message}` |
| `command.request` | deck → agent | `requestId: "<boot nonce hex>-<n>"`, `{action, value?}`; authenticated sessions only |
| `command.result` | agent → deck | same `requestId`; `{ok, action, appliedValue?, error?}` |
| `media.art_request` | deck → agent | `{artId}` — the snapshot names a cover the deck never received; the agent pushes the current cover again (binary `PCDA` frames). Authenticated sessions only; agents < 0.4.7 answer `unknown_type` |
| `weather.search` | deck → agent | `{q, lang: "es"|"en"}` city search (≥ 2 chars); at most one weather message per second per deck |
| `weather.results` | agent → deck | `{q, results: [{name, region, lat, lon}]}` (≤ 6) or `{q, error}` |
| `weather.request` | deck → agent | `{lat, lon}` forecast for the deck's chosen place (on connect, then every 10 min) |
| `weather.data` | agent → deck | compact forecast (below) or `{error}`. Agents < 0.5 answer `unknown_type` |
| `crypto.request` | deck → agent | `{coins: [{p, g}], cur}` — ≤ 12 Binance pairs `p` (`^[A-Z0-9]{1,12}USDT$`) with an optional CoinGecko id `g` (`^[a-z0-9-]{1,48}$`); invalid entries are dropped, extra ones ignored. `cur` ∈ `USD EUR MXN ARS COP BRL` (else USD). At most one every 2 s per deck (extras dropped) |
| `crypto.data` | agent → deck | market snapshot (below) or `{cur, error: "bad_request"\|"unavailable"}` |
| `crypto.chart` | deck → agent | `{p, r, cur}` candles for one pair; `r` ∈ `1h 24h 7d 1m 3m 1y all`. At most one every 0.3 s per deck (extras dropped: retry after a timeout) |
| `crypto.candles` | agent → deck | `{p, r, cur, c}` (below), `{p, r, error: "bad_request"}` or `{p, r, cur, error: "unavailable"}`. Crypto messages: authenticated sessions only; agents < 0.6 answer `unknown_type` |

### Actions (allow-list; anything else → `unknown_action`)

| Action | Value | Effect |
|---|---|---|
| `volume.set` | int 0–100 | default playback device master volume (unmutes when > 0); `appliedValue` = actual level |
| `media.play_pause` / `media.next` / `media.previous` | — | Windows media session (SMTC: Spotify, browser players, VLC…), media-key fallback |
| `media.seek` | int 0–86400 (s) | jump to that position in the current track (players that allow it: see `media.canSeek`); `appliedValue` = requested position |
| `session.lock` | — | locks the Windows session |
| `system.shutdown` / `system.restart` | — | only listed when the PC owner enables them in the PC app (off by default); Windows `shutdown /s|/r /t 10` with a visible notice |

Duplicate `requestId` from the same device returns the stored result without re-executing; > 8 commands/s → `rate_limited`.
App launch / arbitrary execution is not implemented.

## `telemetry.snapshot` payload

Missing / unsupported metrics are **`null`, never `0`**. Floats are rounded to 0.1.

```json
{
  "audio": { "volumePct": 65, "muted": false },
  "media": { "title": "…", "artist": "…", "app": "Chrome", "playing": true, "posS": 38, "durS": 159,
             "canSeek": true, "artId": 3, "color": "#4A6E8C" },
  "battery": { "pct": 79, "plugged": false, "charging": false, "secsLeft": 5400,
               "remainingWh": 62.7, "fullWh": 79.4, "rateW": -14.2 },
  "cpu": { "name": "…", "tempC": 68.2, "tempSensor": "/intelcpu/0/temperature/26", "coreMaxTempC": 74.0,
           "loadPct": 42.5, "freqMHz": 5400, "packagePowerW": 154.2 },
  "gpu": { "name": "…", "tempC": 54.1, "loadPct": 31.0, "vramUsedMiB": 4200, "vramTotalMiB": 12227, "boardPowerW": 120.3 },
  "ram": { "usedMiB": 24371, "totalMiB": 65536 },
  "storage": [ { "name": "…", "tempC": 41.0 } ],
  "fans": [ { "sensorId": "/lpc/nct6799d/0/fan/0", "label": "CPU Fan", "rpm": 1200 } ],
  "temps": [ { "name": "GPU Memory Junction", "tempC": 62.0, "stale": false } ],
  "notes": [ "not running as administrator: …" ],
  "clock": { "epoch": 1794431100, "utcOffsetMin": 60 }
}
```

- `packagePowerW` is **CPU package power**, never whole-PC consumption; `boardPowerW` is GPU power.
- `storage` ≤ 4 entries, `fans` ≤ 8, `temps` ≤ 6 (extra valid sensors, names ≤ 23 bytes), names ≤ 39 bytes.
- `battery` is `null` on PCs without a battery. `plugged`/`charging` may be `null` (unknown);
  `secsLeft` only while on battery; `rateW` > 0 charging, < 0 discharging; energy fields from
  Windows `root\WMI BatteryStatus` (null when unavailable).
- Display strings (titles, names) only use glyphs the deck fonts contain (Latin-1/Extended, Cyrillic,
  Vietnamese, typographic punctuation); the agent folds or drops anything else (`textfit.py`).
- `cpu.tempSensorName` names the sensor behind `tempC` (e.g. `CPU Package`, `CPU (EC)`).
- `clock` (agent ≥ 0.5) is the PC's clock: UTC epoch seconds and its current UTC offset (DST included).
- Nothing in this section is sent before the client is authenticated.

## `weather.data` payload

From [Open-Meteo](https://open-meteo.com) (free, no key), cached 10 min per place by the agent.
Times are the place's local time; temperatures °C (the deck converts), wind km/h; weather codes are WMO.

```json
{
  "lat": 40.42, "lon": -3.7, "tzMin": 60,
  "now": { "t": 16.0, "feels": 13.4, "rh": 69, "wind": 19, "code": 2, "day": 1 },
  "today": { "max": 16, "min": 12, "rise": "07:57", "set": "18:00" },
  "hours": [ { "h": 15, "code": 2, "t": 16, "pop": 0, "day": 1 } ],
  "days": [ { "wd": 4, "code": 3, "max": 16, "min": 11, "pop": 23 } ]
}
```

- `hours`: the next 8 hours starting with the current one; `days`: up to 5 days after today (`wd` 0 = Monday).
- `pop` = precipitation probability %. Any field may be `null`.

## `crypto.data` / `crypto.candles` payloads

From public endpoints (no key): [Binance market data](https://data-api.binance.vision) (prices, 24 h stats,
sparklines, charts, fx), [CoinGecko](https://www.coingecko.com) (market cap, rank, ATH, global market) and
[alternative.me](https://alternative.me/crypto/fear-and-greed-index/) (Fear & Greed). Money values are USD(T)
upstream × `fx` (USD → `cur`), rounded to 6 significant digits; percentages to 0.01. Numbers may arrive as
integers (`73100`) or in exponent form (`1.23457e-05`). Missing values are `null`.

```json
{
  "cur": "EUR", "fx": 0.921342,
  "coins": [ { "p": "BTCUSDT", "price": 73891.2, "chg": -1.23, "chgAbs": -920.12, "hi": 75200.1, "lo": 73100,
               "vol": 29100000000, "mc": 1460000000000, "rank": 1, "ath": 116000, "spark": [12, 15, 40, 100, 0, 37] } ],
  "global": { "mcap": 2900000000000, "mcapChg": 1.2, "btcDom": 57.3, "fng": 63, "fngClass": "Greed" },
  "ts": 1794431100
}
```

- `coins` in request order. `price`, `chgAbs` (24 h change), `hi`/`lo` (24 h), `vol` (24 h quote volume): Binance
  24 h ticker (cached 10 s); `chg` = 24 h change % (not converted). A pair Binance does not list stays in the
  list with `price: null` (and the other ticker fields `null`, `spark: []`).
- `spark`: last 24 h of 30 min closes as ints 0–100 (min → 0, max → 100, flat → 50), oldest first, ≤ 48;
  `[]` if unavailable (cached 5 min).
- `mc`, `rank`, `ath`: CoinGecko (cached 10 min; `null` without `g`). `global`: total market cap, its 24 h
  change %, BTC dominance % (CoinGecko, 10 min) and the Fear & Greed index 0–100 with its label ≤ 19 chars
  (30 min); any field may be `null`, `global` itself is `null` if all of it failed. A CoinGecko 429 pauses it 20 min.
- `fx`: Binance `EURUSDT` (1/p) or `USDTMXN` / `USDTARS` / `USDTCOP` / `USDTBRL` (p), cached 60 s. When it
  cannot be obtained the reply is in USD (`"cur": "USD", "fx": 1`).
- `ts`: agent epoch seconds. `error`: `bad_request` (no valid coin) or `unavailable` (Binance unreachable).

```json
{ "p": "BTCUSDT", "r": "24h", "cur": "EUR", "c": [ [1794478500, 73745.9, 73912.3, 73741, 73891.2, 21460000] ] }
```

- `c`: rows `[t, o, h, l, c, v]`, oldest first, ≤ 120. `t` = candle open time (unix seconds), OHLC converted
  (6 significant digits), `v` = quote volume converted (4 significant digits). The last row is the candle in progress.
- Ranges: `1h` 60 × 1 min · `24h` 96 × 15 min · `7d` 84 × 2 h · `1m` 120 × 6 h · `3m` 90 × 1 day ·
  `1y` 120 × 3 days · `all` weekly candles merged into ≤ 120 buckets aligned to the newest (the oldest
  bucket may be partial). Cached 30 s (`1h`) to 6 h (`all`); the currency is applied per request.
- Both messages stay well under the frame limit (12 coins ≈ 4 KB, 120 candles ≈ 7.5 KB).

## Discovery

mDNS service `_pcdeck._tcp.local.`, TXT `id`, `name`, `v`. Discovery does not grant access.

## Connection rules

- Deck: STALE after 4 s without a snapshot, OFFLINE after 12 s; reconnect with exponential
  backoff (1 s → 30 s cap, ±20 % jitter).
- Agent: accepts clients only from private / loopback / link-local addresses; max 4 clients.
