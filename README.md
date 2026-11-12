# PC Control Deck — desktop app

The PC side of **PC Control Deck**: a small tray app for **Windows, Linux and macOS** that reads the
computer's sensors, media session, weather and crypto prices, and serves them over a paired,
**LAN-only** WebSocket to an ESP32 touch deck. No cloud, no account.

One app serves every deck that speaks protocol v1:

| Deck | Repository |
|---|---|
| Guition **JC4827W543** — 4.3" 480×272 landscape (navigation rail, Crypto tab, setup assistant) | [PC-Control-Deck-ESP32-JC4827W543](https://github.com/crazyramirez/PC-Control-Deck-ESP32-JC4827W543) |
| Guition **JC3248W535** — 3.5" 320×480 portrait (bottom tabs, Crypto tab from firmware 0.9.0, setup wizard) | [PC-Control-Deck-ESP32-JC3248W535](https://github.com/crazyramirez/PC-Control-Deck-ESP32-JC3248W535) |

The deck repositories hold only their firmware; **the app is developed and released here**, and
the decks embed this repository's protocol fixtures in their firmware tests. Weather needs app 0.5
or later, the Crypto tab 0.6 or later.

Current version: **0.7.0** — see [CHANGELOG](docs/CHANGELOG.md).

---

## Contents

1. [Install](#install)
2. [Pair a deck](#pair-a-deck)
3. [The app](#the-app)
4. [What it measures and controls](#what-it-measures-and-controls)
5. [Security and privacy](#security-and-privacy)
6. [Building from source](#building-from-source)
7. [Changing the protocol](#changing-the-protocol)

---

## Install

Download the build for your system from the [Releases](https://github.com/crazyramirez/PC-Control-Deck-Agent/releases)
(or the artifacts of the latest [build run](https://github.com/crazyramirez/PC-Control-Deck-Agent/actions/workflows/build.yml)).

| | File | Install | Notes |
|---|---|---|---|
| **Windows 10/11** | `PCControlDeck-Setup-<ver>.exe` (or `…-windows-x64-portable.zip`) | Run the installer (admin): tray app, optional start with Windows (elevated), firewall rules for **private networks only** | — |
| **Linux** (x86_64) | `PCControlDeck-<ver>-linux-x86_64.tar.gz` | `tar xzf PCControlDeck-*.tar.gz && cd PCControlDeck-*/ && ./install.sh` — per user, no root: app in `~/.local/opt/pcdeck`, menu entry and icon; `./install.sh --uninstall` removes them | Built on Ubuntu 22.04 (glibc ≥ 2.35). Qt needs `libxcb-cursor0` (`sudo apt install libxcb-cursor0`). The tray needs a StatusNotifier host (GNOME: the AppIndicator extension, preinstalled on Ubuntu). If a firewall is active, allow TCP 8765 and UDP 5353 from your LAN (e.g. `sudo ufw allow from 192.168.0.0/16 to any port 8765 proto tcp`). |
| **macOS 14+** | `PCControlDeck-<ver>-macos-arm64.dmg` (Apple Silicon) or `…-macos-x86_64.dmg` (Intel); `.zip` alternatives | Open the `.dmg`, drag **PC Control Deck** to Applications | The app is **not signed**: the first launch is blocked by Gatekeeper. macOS 15+: try to open it once, then *System Settings › Privacy & Security › Open Anyway*; macOS 14: right-click the app › *Open*. (Or: `xattr -dr com.apple.quarantine "/Applications/PC Control Deck.app"`.) Allow incoming connections / local network access when asked, and *Automation* access to Music / Spotify / System Events the first time the deck controls them. |

## Pair a deck

1. In the app click **Pair device**: a 6-digit code appears for 2 minutes.
2. On the deck open *Settings › Pair PC* (or the setup assistant's *Pair PC* step), choose your PC —
   found automatically over mDNS, or type its IP — and enter the code.
3. The deck reconnects by itself from then on, even if the PC's IP address changes.

---

## The app

A tray icon (menu bar on macOS) shows the state (running, devices connected); its window (Spanish or
English, following the system language) has:

- **Pair device** — the one-time 6-digit code with a countdown and how to enter it on the deck.
- **Paired devices** — each deck with its firmware version and *Connected · IP* / *seen 5 min ago*,
  plus **Unpair**.
- **This PC** — address and port, Bonjour discovery, sensor provider, privileges (what needs
  administrator / root on this system).
- **Allow shutting down and restarting the PC from the device** — off by default.
- **Start with Windows** / **Start at login** — Windows: an elevated scheduled task (no UAC prompt at
  logon) so CPU temperature and other privileged sensors are readable; Linux: an entry in
  `~/.config/autostart`; macOS: a LaunchAgent (`~/Library/LaunchAgents/com.pcdeck.agent.plist`).
- **Logs** and **Quit**. Closing the window keeps the app running in the tray / menu bar.

---

## What it measures and controls

**Telemetry (every second)**

| Data | Windows | Linux | macOS |
|---|---|---|---|
| CPU load | LibreHardwareMonitor 0.9.6 | psutil | psutil |
| CPU temperature (+ hottest core) | LibreHardwareMonitor (elevated + PawnIO driver), or the Lenovo Legion embedded controller via WMI (elevated) | hwmon: `coretemp` (Intel), `k10temp` / `zenpower` (AMD), `cpu_thermal` (ARM) | Apple Silicon: IOKit temperature sensors (CPU clusters; M1: die); Intel Macs: SMC |
| CPU clock | LibreHardwareMonitor, or Windows performance counters | cpufreq (highest core) | `powermetrics` — **root only** |
| CPU package power | LibreHardwareMonitor, or the Windows RAPL energy counter | RAPL `intel-rapl:0` — **root or udev rule** (below) | `powermetrics` — **root only** |
| GPU load, temperature, VRAM, power | LibreHardwareMonitor (NVIDIA, AMD, Intel) | NVIDIA: NVML (proprietary driver); AMD: amdgpu sysfs (load, VRAM, edge/junction/memory temperature, power, fan); Intel iGPU: name + clock only | Load: IOAccelerator; temperature: Apple Silicon sensors / AMD driver; VRAM: AMD only (Apple GPUs share RAM); Apple GPU power: `powermetrics` (root) |
| RAM | Windows | psutil | psutil |
| Storage temperatures | LibreHardwareMonitor (elevated) | NVMe, SATA (`drivetemp` module) | Apple Silicon internal SSD |
| Fans, other temperatures | LibreHardwareMonitor / Lenovo EC | hwmon (Super I/O, ACPI, PCH, Wi-Fi, DIMMs, AMD GPU fan) | SMC fans; SoC and battery temperatures |
| Laptop battery | Windows power status + battery WMI | `/sys/class/power_supply` | `ioreg` AppleSmartBattery |
| Volume | Windows Core Audio | PipeWire (`wpctl`) or PulseAudio (`pactl`) | system output volume (AppleScript) |
| Now playing, album art, position, seek | Windows media session (SMTC): any player | MPRIS over D-Bus: any player (Spotify, browsers, VLC, mpv…) | **Music and Spotify only** (AppleScript; macOS has no public "Now Playing" API) |
| Clock (time and UTC offset) | the PC clock | the PC clock | the PC clock |
| Weather for the deck's chosen city | [Open-Meteo](https://open-meteo.com) forecast + geocoding (only when a city is set; cached 10 min) | same | same |
| Crypto prices, charts, market data | Binance public market data, CoinGecko, alternative.me (only while a deck's Crypto tab asks; cached) | same | same |

Missing or unsupported values are sent as `null` and shown as `--` on the deck — never as `0`. The
deck also gets a short note saying why (e.g. *not running as root: CPU package power (RAPL)
unavailable*). On Linux and macOS, album art comes from the URL the player publishes: a local file,
or a download from the player's own image server (e.g. Spotify's CDN), as the player itself does.
Browser tabs without artwork (YouTube, Netflix, Twitch…) get a generated cover.

**Permissions per system**

- **Windows** — CPU temperature needs the app elevated: the installer's *Start with Windows* task does
  that without a UAC prompt. Full CPU sensors also need the PawnIO driver (not installed by the app).
- **Linux** — everything works as a normal user except CPU package power: the RAPL energy counter is
  root-only since Linux 5.10 (the "Platypus" power side channel, CVE-2020-8694). To allow it, add a
  udev rule (this re-opens that side channel to local users — your call):

  ```bash
  echo 'ACTION=="add", SUBSYSTEM=="powercap", KERNEL=="intel-rapl:0", RUN+="/bin/chmod 0444 /sys%p/energy_uj"' \
    | sudo tee /etc/udev/rules.d/90-pcdeck-rapl.rules
  sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=powercap --action=add
  ```

  NVIDIA GPUs need the proprietary driver (NVML); SATA drive temperatures need `sudo modprobe drivetemp`.
  Lock uses `loginctl lock-session`; shutdown/restart use `systemctl` (allowed by polkit for the user
  of the active local session).
- **macOS** — temperatures, fans, GPU load, battery and media work as a normal user. CPU/GPU power and
  the CPU clock come from `powermetrics`, which only runs as root (start the app with `sudo` to get
  them; otherwise they show `--`). Controlling Music / Spotify and shutting down / restarting go
  through Apple Events: macOS asks once for *Automation* permission (*System Settings › Privacy &
  Security › Automation*).

**Commands (allow-list only)** — volume, play/pause, next, previous, seek, lock, and — only if
allowed in the app — shutdown/restart (after a 10 s notice on every system; on Linux/macOS quitting
the app within those 10 s cancels it). There is no generic "run a command" path.

---

## Security and privacy

- Decks connect **only from the local network** (private / loopback / link-local peers, at most 4
  clients). The firewall rules created by the Windows installer apply to *private* networks only.
- The only internet requests are the weather (Open-Meteo: the city searched for and the chosen
  place's coordinates) and crypto data (Binance / CoinGecko / alternative.me: the coin pairs and the
  currency chosen on the deck). No account, no key, nothing about the PC or the user.
- Pairing uses a **6-digit one-time code** (2 minutes, limited attempts). Each deck then gets its own
  random token; the app stores only a hash — protected with DPAPI on Windows, in an owner-only (0600)
  file on Linux and macOS. Unpairing revokes it.
- Commands are validated, de-duplicated and rate-limited; shutdown/restart are opt-in.
- Tokens and pairing codes are never logged.
- Telemetry travels over plain `ws://` inside your LAN — see [SECURITY.md](docs/SECURITY.md) for the
  threat model and its limits.

---

## Building from source

Python 3.13.

Windows (PowerShell):

```powershell
scripts/setup-agent.ps1            # venv, pinned dependencies, verified LibreHardwareMonitor DLLs, tests
scripts/run-agent.ps1 serve --pair # run from source (LAN + loopback, port 8765) and print a pairing code
scripts/build-agent.ps1            # tests + PyInstaller + Inno Setup -> dist/installer/*.exe
```

Linux / macOS (bash; Python 3.13 on `PATH` as `python3.13`, or set `PYTHON`):

```bash
bash scripts/setup-agent.sh                 # venv with the pinned dependencies + tests
bash scripts/run-agent.sh diag              # what this machine exposes: sensor catalog, selection, notes
bash scripts/run-agent.sh serve --pair      # run from source and print a pairing code
PYTHONPATH=src .venv/bin/python -m pcdeck_agent.gui.app   # the tray app from source
bash scripts/build-agent.sh                 # tests + PyInstaller -> dist/release/
                                            #   Linux: PCControlDeck-<ver>-linux-<arch>.tar.gz
                                            #   macOS: PCControlDeck-<ver>-macos-<arch>.zip + .dmg (unsigned)
```

On Ubuntu the build and the app need Qt's runtime libraries:
`sudo apt install libegl1 libxkbcommon-x11-0 libxcb-cursor0 libxcb-icccm4 libxcb-keysyms1 libxcb-shape0`.

Other CLI options: `sample --count 10` (normalized snapshots as JSON lines), `diag --out diag.json`
(redacted sensor report), `serve --fake` (synthetic data), `serve --port 8766 --no-mdns` (a second
instance next to the installed one).

| | Windows | Linux | macOS |
|---|---|---|---|
| Sensors | pythonnet + LibreHardwareMonitor, PDH counters, Lenovo WMI | sysfs / hwmon / powercap, `nvidia-ml-py` (NVML) | ctypes IOKit (IOHID, AppleSMC), `ioreg`, `sysctl`, `system_profiler`, `powermetrics` |
| Media / volume | pycaw/comtypes, PyWinRT (SMTC) | `jeepney` (MPRIS over D-Bus), `wpctl` / `pactl` | `osascript` |
| Paired-device store | DPAPI | 0600 file | 0600 file |
| Package | PyInstaller + Inno Setup | PyInstaller → `.tar.gz` | PyInstaller → `.app` → `.zip` / `.dmg` |

Common: websockets, zeroconf (mDNS), PySide6 (tray app), psutil, Pillow (album art).
Details: [ADR 0002](docs/adr/0002-agent-stack.md), [ADR 0003](docs/adr/0003-pairing-tray-installer.md),
[ADR 0005](docs/adr/0005-cross-platform-agent.md) (Linux and macOS).

**Continuous integration:** [`build.yml`](.github/workflows/build.yml) runs the tests and builds the
app on Windows, macOS arm64 (`macos-latest`), macOS x86_64 (`macos-15-intel`) and Ubuntu 22.04 on
every push; installers / archives are uploaded as artifacts. A push to `main` that changes the
version (`pcdeck_agent.__version__`, with its `## X.Y.Z` CHANGELOG section) publishes them as a
GitHub Release from the same run, tag `vX.Y.Z` included (don't push tags by hand: the commit would
build twice).

```
src/pcdeck_agent/   server, protocol, pairing/credentials, hardware providers (Windows, Linux, macOS),
                    media/commands, weather, crypto, tray GUI
tests/              pytest (runs on Windows, Linux and macOS)
shared/             protocol fixtures, also embedded in the deck firmware tests
packaging/          PyInstaller spec, Inno Setup script, icons, Linux .desktop + install.sh
scripts/            setup / run / build helpers
docs/               PROTOCOL, SECURITY, ADRs, CHANGELOG
```

---

## Changing the protocol

1. Update [PROTOCOL.md](docs/PROTOCOL.md) and the fixtures in [`shared/fixtures`](shared/fixtures)
   (the app's tests read them), and keep protocol v1 backwards compatible: decks ignore unknown
   fields and message types.
2. In each deck repository (cloned next to this one, e.g. `D:\DEV\PC-Control-Deck-Agent` and
   `D:\DEV\PC Control Deck - ESP32-JC4827W543`), run `python firmware/tools/gen_fixtures.py` to
   re-embed the fixtures in the firmware tests (`--agent <dir>` if this repository is elsewhere) and
   commit `fixtures.h`. The JC4827W543 CI checks it against this repository's `main`
   (`gen_fixtures.py --check`).
