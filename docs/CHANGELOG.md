# Changelog — PC Control Deck desktop app

Until 0.7.0 the app lived in the `agent/` folder of the JC3248W535 deck repository; this repository
keeps that history. Deck firmware changes are listed in each deck repository.

## 0.7.0 — Linux and macOS

The app now runs on **Linux** and **macOS** as well as Windows (same tray app, same protocol, same
pairing; deck firmware unchanged). Details and permissions: [ADR 0005](adr/0005-cross-platform-agent.md).

- **Platform layer** (`platforms.py`): the hardware provider, battery reader and command backend are
  picked from the OS at run time. Windows keeps its exact code paths (LibreHardwareMonitor, Lenovo EC,
  perf counters, Core Audio + SMTC, DPAPI, scheduled tasks).
- **Linux provider:** CPU load/clock/model, temperatures from hwmon (coretemp, k10temp/zenpower,
  cpu_thermal) with core max, CPU package power from RAPL (root-only since Linux 5.10: null + note
  otherwise), NVIDIA through NVML, AMD through amdgpu sysfs (load, VRAM, edge/junction/memory
  temperatures, power, fan), Intel iGPU name/clock, NVMe/SATA temperatures, fans, other hwmon
  temperatures, battery from `/sys/class/power_supply` (Hyper-V/WSL virtual batteries: percentage only).
- **macOS provider:** psutil load/RAM; Apple Silicon temperatures through IOHID (CPU clusters, GPU,
  SSD, battery), Intel Macs through the SMC; fans from the SMC; GPU load (and AMD VRAM/temperature)
  from IOAccelerator; CPU/GPU power and CPU clock from `powermetrics` when running as root (null +
  note otherwise); battery from `ioreg AppleSmartBattery`.
- **Media / volume / lock / power:** Linux MPRIS over D-Bus (any player, same sticky selection as
  Windows, `mpris:artUrl` covers, seek), `wpctl` / `pactl` volume, `loginctl` lock, `systemctl`
  shutdown/restart after a 10 s notice. macOS: Music and Spotify through AppleScript (state, seek,
  artwork), system volume, lock screen, System Events shutdown/restart after a 10 s notice.
- **Credentials:** DPAPI on Windows; an owner-only (0600) file elsewhere (it holds token hashes only).
  Config in `~/.config/pcdeck` / `~/Library/Application Support/PC Control Deck`, logs in
  `~/.local/state/pcdeck/logs` / `~/Library/Logs/PC Control Deck`.
- **Tray app:** start at login through `~/.config/autostart` (Linux) or a LaunchAgent (macOS); menu-bar
  only app on macOS (`LSUIElement`); ES/EN strings adapted per OS.
- **Packaging / CI:** one PyInstaller spec for the three systems (macOS `.app` → zip + unsigned `.dmg`,
  Linux `.tar.gz` with `.desktop`, icon and `install.sh`), `scripts/build-agent.sh`, `setup-agent.sh`,
  `run-agent.sh`. GitHub Actions `build.yml`: tests + builds on Windows, macOS arm64/x86_64 and
  Ubuntu; GitHub Release on `v*` tags.
- Generated covers for browser services use the service's initial where Windows' Segoe icon font
  does not exist.
- Own repository, used by the deck repositories as the `agent/` submodule.

## 0.6.0 — Crypto

- `crypto.request` → `crypto.data` (prices, 24 h change, high/low, volume, market cap, rank, ATH,
  sparkline; global market cap, BTC dominance, Fear & Greed) and `crypto.chart` → `crypto.candles`
  (1h, 24h, 7d, 1m, 3m, 1y, all; up to 120 candles). Binance public market data, CoinGecko,
  alternative.me; cached, rate-limited, converted to the chosen currency.
- Fixtures `crypto_data.json` / `crypto_candles.json`.

## 0.5.0 — Weather and clock

- Open-Meteo weather + geocoding (`weather.search` / `weather.request` → `weather.results` /
  `weather.data`, cached); PC clock in every snapshot (`clock`).

## 0.4.7

- Album art no longer stays grey: players publish a new track's title before its thumbnail, so the
  app re-reads the cover every 1.5 s for 12 s after a track change (up to 60 s while there is none).
- New `media.art_request` (deck → app): the deck asks again for a cover it never got and the app
  resends it.

## 0.4.6

- Online services in a browser tab without media metadata (Instagram, TikTok, Netflix, Twitch, X,
  Prime Video, Disney+, Max, Facebook, Vimeo, SoundCloud, Spotify/Deezer/Tidal/Apple Music web…)
  are recognised from the tab title: generated cover (brand colours + icon + name, drawn locally),
  source shown as "<Service> · <browser>", "- YouTube"-style suffixes removed from titles.

## 0.4.5

- Play only acts on the detected player again (media key when there is none); launching a player
  app from the deck (0.4.3/0.4.4) was removed.
- Installed web apps show their own name instead of "Chrome".

## 0.4.3

- Sticky player selection: another app only takes over after playing for 3 s; a 4 s grace hold
  during track changes.

## 0.4.2

- CPU clock and CPU package power without the PawnIO driver: Windows performance counters
  (`% Processor Performance` × `Processor Frequency`, `Energy Meter(RAPL_Package0_PKG)\Power`), used
  only when LibreHardwareMonitor has no value.

## 0.4.1

- Album art (150 px RGB565 binary frames), seek (`media.seek`, `media.canSeek`), silent success for
  media/volume commands.
- Lock / restart / shut down; shutdown and restart only when enabled in the app, off by default.
- Characters the deck fonts lack (emoji, CJK, Greek) are folded so the deck never shows empty boxes.
- Laptop battery in the snapshot: charge %, charging / plugged in / time left, remaining and full
  energy (Wh).

## 0.3.0

- Remote controls: volume (Core Audio), play/pause/next/previous through the Windows media session
  (any SMTC player incl. browser web players; media-key fallback), session lock. Allow-list, value
  validation, per-device de-duplication and rate limit.
- Snapshots carry PC volume/mute and "now playing" (title, artist, app, state).
- Lenovo Legion 2023+ EC sensors via `LENOVO_OTHER_METHOD` capability IDs (CPU temp, fans).
- The window sizes itself exactly to its content.

## 0.2.0

- Pairing with a one-time code + per-device token auth (DPAPI); mDNS discovery (`_pcdeck._tcp`).
- Tray app (PySide6): status, pairing code with countdown, paired devices with unpair, this-PC info,
  start with Windows. Inno Setup installer with elevated scheduled tasks and private-only firewall rules.
- CPU temperature from the Lenovo Legion EC (GameZone WMI) when elevated; `temps` list of extra sensors.

## 0.2.0-hito3 (unreleased)

- Python 3.13 (ADR 0002): LibreHardwareMonitor 0.9.6 via pythonnet, sensor normalizer (null never 0,
  CPU 0 W → null, stale detection, discrete-GPU selection), catalog + redacted diagnostics, WebSocket
  server (read-only, LAN-only peers, hello/rate/size limits).
- Protocol v1 documented (PROTOCOL.md) with shared fixtures used by the app and firmware tests.
