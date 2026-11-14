# Changelog — DECKY Agent (the desktop app of DECKY)

The app was "PC Control Deck" until 0.10 and "Control Deck" until 1.0. Until 0.7.0 it lived in the
`agent/` folder of the JC3248W535 deck repository; this repository keeps that history. Deck firmware
changes are listed in [DECKY-Firmware](https://github.com/crazyramirez/DECKY-Firmware/blob/main/docs/CHANGELOG.md).

## 1.1.3 — Latest deck firmware: more cities on Today

- **Bundled deck firmware: 1.3.0** for every deck. Today holds up to 6 cities: swipe sideways for
  the next one (its local time, weather and picture), or open the *Cities* sheet, a world clock
  with every city's time and weather where you add, remove or pick the main one. The app serves
  each city's forecast as it already did for the one place: nothing changes on the PC.

## 1.1.2 — Latest deck firmware: no random restarts after power-on

- **Bundled deck firmware: 1.2.2** for every deck. A deck could restart by itself a few seconds
  after power-on (its first web request meeting the clock's time-server lookup); that is fixed. When
  the screen comes back on with a full-screen sheet open, the whole screen is redrawn.

## 1.1.1 — Latest deck firmware: smoother ESP32-4848S040

- **Bundled deck firmware: 1.2.1** for every deck. The ESP32-4848S040 answers much faster to small
  changes (a key, a button, the clock): it no longer copies its whole screen to the panel for each
  of them. On every deck, the page under a full-screen sheet (Wi-Fi and its keyboard, place search,
  pairing, the setup assistant, the screensaver) is no longer drawn while hidden.

## 1.1.0 — A fourth deck: Guition ESP32-4848S040

- **The Guition ESP32-4848S040** (4" 480×480 square) joins the decks: *Update deck firmware*
  recognises it on USB and flashes it. Its USB-C is a CH340 USB-UART bridge, so the updater now
  also looks at CH340 / CH343 / CP210x ports (only a deck answers there; nothing is flashed unless
  you pick it). Pairing works as with the other decks.
- **Bundled deck firmware: 1.2.0** for every deck, the ESP32-4848S040 included. On every deck the
  setup assistant now runs at full brightness; the level set in the deck's *Settings* comes back
  when it closes.

## 1.0.1 — Latest deck firmware: screensaver on demand

- **Bundled deck firmware: 1.1.0** for every deck. A double tap on the status corner at the top
  right of the deck (its battery and the PC link LED) starts the screensaver at once, when it is
  enabled in the deck's *Settings › Screensaver*.

## 1.0.0 — DECKY Agent; one firmware repository for every deck

- **The app is now called DECKY Agent** (the project is **DECKY**). The new name shows in the
  window, the tray, the notifications, the installer, the Start-menu and desktop shortcuts, the
  scheduled tasks, the macOS app, the Linux menu entry and the power-action notices.
- **Upgrading keeps everything**: paired decks and settings stay in the same data folders. The
  setup removes the shortcuts, scheduled tasks and firewall rule of the old names ("PC Control
  Deck", "Control Deck"); on Linux the menu and autostart entries are renamed at the first start;
  on macOS an app updated by itself keeps its old file name until it is installed again from the
  `.dmg`.
- **USB firmware updates from one repository:** the decks' firmware now comes from
  [DECKY-Firmware](https://github.com/crazyramirez/DECKY-Firmware) (one version, one release with
  every deck's images); the updater picks the image named after the deck it recognises.
- **Bundled deck firmware: 1.0.0** for the JC1060P470C (both ESP32-P4 revisions), the JC4827W543
  and the JC3248W535: the decks say *DECKY*, and the pairing steps name DECKY Agent.
- The app's repository is now **DECKY-Agent** (GitHub redirects the old address, so earlier
  versions keep updating themselves).

## 0.11.0 — Latest deck firmware: Today follows the weather

- **Bundled deck firmware:** JC1060P470C 0.13.0, JC4827W543 0.13.0 and JC3248W535 0.14.0. The
  Today page puts a photograph of the current weather behind the clock (sun, clouds, fog, rain,
  storm, snow…, by day or by night; a blue veil when it is cold, a warm one when it is hot); on the
  7" deck rain and snow fall over it and storms flash. *Settings › Time & weather › Weather
  background* on the deck turns it off.

## 0.10.1 — Latest deck firmware

- **Bundled deck firmware:** JC1060P470C 0.12.6, JC4827W543 0.12.4 and JC3248W535 0.13.3. While
  joining a Wi-Fi network the deck now says *Connecting to <network>…* instead of a false *Could
  not connect* (shown before when the first tries failed).

## 0.10.0 — Control Deck; latest deck firmware

- **The app is now called Control Deck.** The new name shows in the window, the tray, the
  installer, the Start-menu and desktop shortcuts, the scheduled tasks, the macOS app, the Linux
  menu entry and the notifications.
- **Upgrading keeps the paired decks and settings**: the data folders keep their names.
  - **Windows:** the setup removes the old shortcuts, scheduled tasks and firewall rule named
    "PC Control Deck", and an upgraded install keeps its folder.
  - **Linux:** the menu and autostart entries are renamed at the first start.
  - **macOS:** an app updated by itself keeps its old file name until it is installed again from
    the `.dmg`.
- **Bundled deck firmware:** JC1060P470C 0.12.5, JC4827W543 0.12.3 (setup assistant without the
  coins step; the city step needs only Wi-Fi) and JC3248W535 0.13.2.

## 0.9.0 — The app updates itself

The app installs its own new releases from GitHub, on Windows, macOS and Linux
([ADR 0007](adr/0007-app-self-update.md)).

- It checks a minute after starting and every 6 hours. When the latest release is newer and has a
  build for this system, the app downloads it (size and GitHub SHA-256 checked) and installs it.
  The new version starts by itself, and a notification confirms *updated to X*.
  - **Windows:** the setup runs silently, keeps the start-with-Windows and firewall choices, and
    starts the app again (new `/AUTOUPDATE=1` option). No prompt when the app runs elevated, which
    it does when started by Windows or from its Start-menu entry.
  - **macOS:** the new `.app` replaces the running one.
  - **Linux:** the new folder replaces the one in `~/.local/opt/pcdeck`.
- *This PC*: **Update the app automatically** (on by default), the update status, and **Check for
  updates** / **Update to X**. With automatic updates off, the app tells you and waits for the
  click.
- Never in the middle of a USB firmware update. A failed attempt is not retried by itself for a
  day.
- The Windows portable zip, a read-only location and running from source only report the new
  version (with a link to the downloads).
- Testing aid: `PCDECK_UPDATE_FROM=0.0.1` makes an installed build update itself to the published
  release.

## 0.8.0 — Deck firmware over USB

Install or update a deck's firmware from the app, on Windows, macOS and Linux
([ADR 0006](adr/0006-usb-firmware-updates.md)).

- **Paired devices › Update firmware over USB…** lists the decks plugged in over USB: model, port,
  installed firmware and the latest one. **Update** keeps Wi-Fi, pairing and settings; **Clean
  install** erases them. Boards without the deck firmware get a full install once you pick the
  model.
- **Automatic choice of image:** the deck names its model (`id` console command; firmware without
  it is recognised from `status`). esptool checks the chip (ESP32-S3 / ESP32-P4), and on the
  JC1060P470C the build for its P4 silicon revision (v1.x / v3.x) is picked.
- **The latest firmware of every deck ships inside the app** (this release: JC1060P470C 0.12.4,
  JC4827W543 0.12.2, JC3248W535 0.13.2), so updates work offline. When GitHub has a newer firmware
  release, the app downloads and uses it.
- **Checked twice:** at build time and before writing. Each image must match GitHub's SHA-256 and be
  a sound ESP image for the deck's chip (partition table, bootloader and app headers, checksum,
  hash). A bad or missing image stops the app build.
- esptool 5.6.0 as a library (with pyserial). Linux: the user needs serial port access (`dialout`).
- Developer tool: `tools/fetch_release.py` puts the latest published app release (Windows, Linux,
  macOS builds from CI, SHA-256 checked) into `dist/release/<system>/` and clears older versions
  from `dist/`.

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
- Own repository, shared by the JC4827W543 and JC3248W535 decks (they embed its protocol fixtures).

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
