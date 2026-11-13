# Security model (simplified) — status after Hito 3

## Current posture: **LAN only, paired devices only, no remote actions yet**

| Area | State (0.2.0 agent / 0.3.0 firmware) |
|---|---|
| Transport | plain `ws://` — telemetry is **unencrypted** on the LAN. Do not use on public / shared networks. |
| Pairing | one-time 6-digit code shown in the PC app (120 s, single use, 5 tries), entered on the device. |
| Authentication | per-device 256-bit token; agent stores only its SHA-256, DPAPI-encrypted (`devices.dat`). Unauthenticated clients get no telemetry. Failed attempts limited per session (5) and per peer (10/min). Devices can be revoked from the PC app. |
| Commands | **not executed**. `command.request` → `not_supported` (authenticated) / `unauthorized`. There is no shell/exec path in the code. |
| Exposure | agent binds to the primary LAN IPv4 + loopback (config `bind: ["auto"]`), never `0.0.0.0` by default; peers outside private/loopback/link-local ranges are closed (1008); max 4 clients; 16 KiB frame cap; 20 msg/s rate limit; `hello` required within 5 s. No UPnP, no port forwarding. |
| Firewall | the agent never creates firewall rules. Observed on the dev PC: pre-existing inbound Allow rules for `python313\python.exe` on **Private and Public** profiles (they apply to any Python script; owner should remove the Public ones). On first `serve`, Windows shows its own prompt for `python.exe`: allow **Private networks only**. |
| Deck secrets | Wi-Fi password stored in NVS namespace `pcd_net`, **not encrypted** (flash encryption not enabled on this board). Physical access to the device = access to the Wi-Fi key. Never printed on serial (`status` shows `<set>`). `net clear` erases it. |
| Agent secrets | token hashes in DPAPI-protected `devices.dat`; config and logs contain no secrets; tokens and codes are never logged. |
| Installer | admin install; registers elevated scheduled tasks for the installing user (sensor access, no UAC prompts) and, if the option is kept, program-scoped firewall rules for the **private** profile only (TCP 8765, UDP 5353 mDNS). Uninstall removes both. |
| Privileges | installed build runs elevated through its scheduled task (owner decision, ADR 0003); dev runs are unelevated and report missing sensors. No kernel driver is installed (PawnIO not used). |

## Threats (spec §8) and plan

| Threat | Mitigation now | Planned |
|---|---|---|
| Unauthorized LAN user | read-only, LAN-only peers, rate limits | Hito 4: visible approval on PC + one-time code, per-device random credential (DPAPI on PC), revocation list |
| Spoofed agent / deck | none (no auth) | Hito 4: mutual credential; later `wss://` with pinned certificate |
| Interception / replay | none (no TLS) | Hito 4: per-message requestId + nonce/timestamp window; real protection needs TLS (`wss://`) |
| Shutdown without consent | power actions not implemented; deck requires explicit confirm dialog | Hito 4: disabled by default on the agent, opt-in per device |
| Exposure to public networks | private-range peer filter, LAN bind | firewall guidance in SETUP_WINDOWS.md |
| Credential theft | no credentials yet; Wi-Fi key unencrypted in NVS (documented) | evaluate NVS encryption with HMAC-based keys (eFuse, irreversible: owner decision) |

## Tests covering this

- `tests/test_server.py`: no hello → close; wrong first message; unsupported version; command → unauthorized NACK with
  correlation; malformed frame; oversized frame; rate limit; max clients; private-peer filter.
- `tests/test_protocol.py`: malformed / unknown type / version / size limits.
- [DECKY-Firmware](https://github.com/crazyramirez/DECKY-Firmware), `firmware/test/test_protocol`: same rejections on the deck parser; null → NaN (never 0).

## Internet access (weather, crypto)

Everything between the deck and the PC stays on the LAN. The only internet requests are made by
the PC app, and only when a deck tab asks for them.

Today tab, only after a city has been chosen on the deck:

- `geocoding-api.open-meteo.com` — the text typed in the city search (≤ 60 chars) and the UI language.
- `api.open-meteo.com` — the chosen place's coordinates (rounded to 0.01°), every 10 min at most.

Crypto tab, only when it asks for prices or a chart (agent ≥ 0.6):

- `data-api.binance.vision` — the coin pairs shown on the deck (e.g. `BTCUSDT`) and, for the
  display currency, its fixed exchange-rate pair (`EURUSDT`, `USDTBRL`…).
- `api.coingecko.com` — the CoinGecko ids of those coins (market cap, rank, all-time high) and the
  global market figures.
- `api.alternative.me` — the Fear & Greed index (fixed URL, no parameters).

What leaves the PC for the Crypto tab is only the coin pairs / ids and the chosen currency, cached
(10 s to 6 h) so refreshes and several decks don't multiply requests; nothing about the user or the PC.

No account, key, device id, PC name or user data is sent. The deck itself only contacts the PC
and a public NTP server (time).
