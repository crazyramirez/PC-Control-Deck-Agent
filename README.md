# PC Control Deck — desktop app (agent)

The PC side of **PC Control Deck**: a small tray app for **Windows, Linux and macOS** that reads the
computer's sensors, media session, weather and crypto prices, and serves them over a paired,
**LAN-only** WebSocket to an ESP32 touch deck. One app serves every deck that speaks protocol v1:

| Deck | Repository |
|---|---|
| Guition **JC4827W543** — 4.3" 480×272 landscape (navigation rail, Crypto tab, setup assistant) | [PC-Control-Deck---ESP32-JC4827W543](https://github.com/crazyramirez/PC-Control-Deck---ESP32-JC4827W543) |
| Guition **JC3248W535** — 3.5" 320×480 portrait | [PC-Control-DECK---PC-Control-Deck---ESP32-JC3248W535](https://github.com/crazyramirez/PC-Control-DECK---PC-Control-Deck---ESP32-JC3248W535) |

Both deck repositories include this one as a git submodule in `agent/` (protocol fixtures and docs,
pinned to the version each firmware was tested with). **The app itself is developed and released
here.**

Current version: **0.7.0** — see [CHANGELOG](docs/CHANGELOG.md).

---

## Install

Download the build for your system from the [Releases](https://github.com/crazyramirez/PC-Control-Deck-Agent/releases)
(or from the artifacts of the latest [build](https://github.com/crazyramirez/PC-Control-Deck-Agent/actions) run):

| System | Package | Notes |
|---|---|---|
| **Windows 10/11** (x64) | `PCControlDeck-Setup-<ver>.exe` (installer) or `…-windows-x64-portable.zip` | The installer can start the app with Windows **elevated** (scheduled task, no UAC prompt at logon) so CPU temperature is readable, and adds firewall rules for **private networks only** |
| **macOS 14+** (Apple Silicon / Intel) | `PCControlDeck-<ver>-macos-<arch>.dmg` / `.zip` | Unsigned: the first time, right-click the app › **Open** (or *System Settings › Privacy & Security › Open Anyway*). Menu-bar app (no Dock icon). Allow *Automation* for Music / Spotify / System Events when asked |
| **Linux** x86_64 (glibc 2.34+: Ubuntu 22.04, Fedora 35, Debian 12 or newer) | `PCControlDeck-<ver>-linux-x86_64.tar.gz` | Unpack and run `./install.sh` (installs to `~/.local`, adds the menu entry). A system tray needs the AppIndicator extension on GNOME |

Then pair: in the app click **Pair device** — a 6-digit code appears for 2 minutes — and on the deck
open *Settings › Pair PC* (or the setup assistant), choose your PC (found automatically over mDNS) and
type the code. The deck reconnects by itself from then on.

---

## What it reads and controls

Missing or unsupported values are sent as `null` and shown as `--` on the deck — never as `0`.

| | Windows | Linux | macOS |
|---|---|---|---|
| CPU load / RAM | LibreHardwareMonitor, Windows | psutil | psutil |
| CPU temperature / core max | LibreHardwareMonitor (elevated), Lenovo Legion EC via WMI | hwmon (coretemp, k10temp/zenpower, cpu_thermal) | Apple Silicon: IOHID sensors · Intel Macs: SMC |
| CPU clock | LibreHardwareMonitor / perf counters | sysfs cpufreq | only as root (`powermetrics`), else null |
| CPU package power | LibreHardwareMonitor / perf counters | RAPL — root or a udev rule, else null + note | only as root (`powermetrics`), else null + note |
| GPU load / temp / VRAM / power | NVIDIA, AMD, Intel (LibreHardwareMonitor) | NVIDIA (NVML), AMD (amdgpu sysfs), Intel iGPU name/clock | load (IOAccelerator); temp on Apple Silicon & AMD; VRAM on AMD; Apple GPU power as root |
| Storage temps / fans / extra temps | LibreHardwareMonitor | NVMe & SATA (hwmon) / hwmon fans / hwmon | Apple SSD / SMC fans / SoC & battery sensors |
| Laptop battery | Windows power status + battery WMI | `/sys/class/power_supply` | `ioreg AppleSmartBattery` |
| Now playing, cover art, seek | any app on the media session (SMTC) | any MPRIS player (D-Bus) | Music and Spotify (AppleScript) |
| Volume | Core Audio | `wpctl` (PipeWire) / `pactl` (PulseAudio) | system output volume |
| Lock / shut down / restart | Windows (power actions opt-in, 10 s notice) | `loginctl` / `systemctl` (opt-in, 10 s notice) | lock screen / System Events (opt-in, 10 s notice) |
| Clock | Windows clock | system clock | system clock |
| Weather (city chosen on the deck) | [Open-Meteo](https://open-meteo.com) | same | same |
| Crypto (Crypto tab) | Binance public market data, CoinGecko, alternative.me | same | same |
| Start at login | elevated scheduled task | `~/.config/autostart` | LaunchAgent |
| Paired-device store | token hashes protected with DPAPI | owner-only (0600) file | owner-only (0600) file |

Permissions per OS (RAPL udev rule, `powermetrics`, Automation): [ADR 0005](docs/adr/0005-cross-platform-agent.md).

**Commands (allow-list only):** volume, play/pause, next, previous, seek, lock and — only if allowed
in the app — shutdown/restart. There is no generic "run a command" path.

---

## Security and privacy

- Decks connect **only on the local network** (private / loopback / link-local peers, max 4 clients);
  telemetry travels over plain `ws://` inside your LAN.
- Pairing uses a **6-digit one-time code** (2 minutes, limited attempts). Each deck then gets its own
  random token; the app stores only a hash. Unpairing revokes it.
- Internet requests are made only for the weather (the city you search for, the chosen place's
  coordinates) and the Crypto tab (the coin pairs and the currency you chose) — no account, no key,
  nothing about the PC or the user.
- Commands are validated, de-duplicated and rate-limited; shutdown/restart are opt-in.

Threat model and limits: [SECURITY.md](docs/SECURITY.md). Wire protocol: [PROTOCOL.md](docs/PROTOCOL.md)
(fixtures in [`shared/fixtures`](shared/fixtures), used by these tests and by the deck firmware tests).

---

## Development

Python 3.13.

```powershell
# Windows
scripts/setup-agent.ps1            # venv, pinned dependencies, verified LibreHardwareMonitor DLLs, tests
scripts/run-agent.ps1 serve --pair # run from source (LAN + loopback, port 8765) and print a pairing code
scripts/build-agent.ps1            # tests + PyInstaller + Inno Setup -> dist/installer/*.exe
```

```bash
# Linux / macOS
bash scripts/setup-agent.sh        # venv + dependencies + tests (PYTHON=/path/to/python3.13 if needed)
bash scripts/run-agent.sh serve --pair
bash scripts/build-agent.sh        # -> dist/release/*.tar.gz | *.zip + *.dmg
```

Other CLI commands: `sample --count 10` (normalized snapshots as JSON lines), `diag --out diag.json`
(redacted sensor report), `serve --fake` (synthetic data), `serve --port 8766 --no-mdns` (side-by-side
runs).

**CI:** [`.github/workflows/build.yml`](.github/workflows/build.yml) runs the tests and builds the app
on Windows, macOS (arm64 + x86_64) and Ubuntu on every push; tags `v*` publish the builds as a GitHub
Release.

**Changing the protocol:** update [PROTOCOL.md](docs/PROTOCOL.md) and `shared/fixtures` here, then bump
the `agent` submodule in the deck repositories and regenerate their firmware fixtures
(`python firmware/tools/gen_fixtures.py`).

```
src/pcdeck_agent/   server, protocol, pairing/credentials, hardware providers, media/commands, weather,
                    crypto, tray GUI
tests/              pytest (runs on Windows, Linux and macOS)
shared/             protocol fixtures shared with the deck firmware
packaging/          PyInstaller spec, Inno Setup script, icons, Linux .desktop + install.sh
scripts/            setup / run / build helpers
docs/               PROTOCOL, SECURITY, ADRs, CHANGELOG
```

Stack: websockets, zeroconf (mDNS), PySide6 (tray app), psutil, Pillow; Windows: pythonnet +
LibreHardwareMonitor 0.9.6, pycaw/comtypes, PyWinRT; Linux: jeepney (D-Bus), nvidia-ml-py; macOS:
ctypes (IOKit/IOHID). [ADR 0002](docs/adr/0002-agent-stack.md),
[ADR 0003](docs/adr/0003-pairing-tray-installer.md), [ADR 0005](docs/adr/0005-cross-platform-agent.md).
