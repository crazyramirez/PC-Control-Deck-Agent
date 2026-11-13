# ADR 0005 — The PC app on Linux and macOS

- **Status:** accepted (owner request, 2026-11-12) · agent 0.7.0
- **Extends:** [ADR 0002](0002-agent-stack.md) (provider split), [ADR 0003](0003-pairing-tray-installer.md) (tray app, installer)

## Context

The agent was Windows-only: LibreHardwareMonitor through pythonnet, Windows performance counters,
Lenovo WMI, Core Audio + the media session (SMTC), DPAPI, scheduled tasks. Everything else (protocol,
pairing, server, mapper, weather, crypto, artwork) is already plain Python. The owner wants the same
app on Linux and macOS, reading as much of the same information as possible, with CI builds for all
three systems.

## Decision

1. **One package, a platform layer.** `pcdeck_agent/platforms.py` picks, from `sys.platform` at call
   time, the hardware provider, the battery reader and the command backend. Windows gets exactly the
   classes it had (`LhmProvider`, `BatteryReader`, `WindowsBackend`); platform modules are imported
   lazily so the package imports everywhere. Linux / macOS providers emit the same `RawSensor`s with
   LibreHardwareMonitor's names ("CPU Package", "Core Max", "GPU Core", "GPU Memory Used"...), so the
   shared mapper, protocol and deck need no change. The only mapper change: `"CPU Die"` appended
   (last) to the CPU temperature priority — LibreHardwareMonitor never produces it.
2. **Dependencies:** standard library + psutil; `jeepney` (pure-Python D-Bus) and `nvidia-ml-py`
   (ctypes NVML) on Linux only, through environment markers. macOS uses `ctypes` for IOKit /
   CoreFoundation and command-line tools (`ioreg`, `sysctl`, `system_profiler`, `powermetrics`,
   `osascript`); no pyobjc.
3. **Never invent values.** Anything a platform cannot read stays `null` and `status().notes` says why
   (sent to the deck like the Windows "not running as administrator" note).

## What each OS reads

| Data | Windows | Linux | macOS |
|---|---|---|---|
| CPU load | LHM | psutil | psutil |
| CPU clock | LHM / perf counters | `scaling_cur_freq` (max core), else `/proc/cpuinfo` | powermetrics (**root**), else null¹ |
| CPU temperature | LHM (admin + PawnIO) / Lenovo EC | hwmon `coretemp` "Package id 0", `k10temp`/`zenpower` Tdie/Tctl, `cpu_thermal` | Apple Silicon: IOHID sensors (max of pACC/eACC, M1: max "PMU tdie"); Intel: SMC TCXC/TC0F/TC0E/TC0D |
| CPU core max | LHM | max of hwmon cores / CCDs | Apple Silicon: max of CPU cluster sensors |
| CPU package power | LHM / RAPL perf counter | RAPL `intel-rapl:0/energy_uj` delta (**root** or udev rule²) | powermetrics "CPU Power" (**root**) |
| GPU | LHM (NVIDIA/AMD/Intel) | NVIDIA: NVML; AMD: `gpu_busy_percent`, `mem_info_vram_*`, hwmon edge/junction/mem, power; Intel: name + clock | IOAccelerator "Device Utilization %"; AMD dGPU: VRAM + temperature; Apple: temperature (IOHID), power (powermetrics, root) |
| RAM | psutil | psutil | psutil |
| Storage temperatures | LHM | hwmon `nvme` (Composite), `drivetemp` (SATA) | Apple Silicon: NAND sensor |
| Fans | LHM / Lenovo EC | every hwmon `fan*_input` (+ AMD GPU fan) | SMC `FNum` / `F<n>Ac` |
| Extra temperatures (≤ 6) | LHM | other hwmon chips (ACPI, PCH, Wi-Fi, DIMMs, Super I/O) | SoC, battery |
| Battery | GetSystemPowerStatus + WMI | `/sys/class/power_supply` | `ioreg -rn AppleSmartBattery` (+ psutil) |
| Now playing / transport / seek | SMTC (any player) | MPRIS over D-Bus (any player) | AppleScript: **Music and Spotify only**³ |
| Album art | SMTC thumbnail | `mpris:artUrl` (file or http(s)) | Spotify `artwork url`, Music artwork data |
| Volume | Core Audio | `wpctl` (PipeWire) or `pactl` (PulseAudio) | `osascript` output volume |
| Lock | LockWorkStation | `loginctl lock-session` (or `xdg-screensaver`) | `SACLockScreenImmediate`, else `pmset displaysleepnow` |
| Shutdown / restart (opt-in) | `shutdown /s|/r /t 10` | notice + `systemctl poweroff|reboot` after 10 s | notice + System Events shut down / restart after 10 s |
| Paired-device store | DPAPI file | 0600 file in `~/.config/pcdeck` | 0600 file in `~/Library/Application Support/PC Control Deck` |
| Start at login | elevated scheduled task | `~/.config/autostart/pcdeck.desktop` | LaunchAgent `com.pcdeck.agent` |

¹ macOS only exposes the *nominal* frequency to normal users (what psutil returns): reporting it as
the current clock would be a fake value. ² RAPL is root-only since Linux 5.10 (CVE-2020-8694, the
"Platypus" power side channel). ³ macOS has no public API for the system-wide Now Playing session.

## Permissions and what stays null

- **Linux, normal user:** everything but CPU package power (note: "not running as root: CPU package
  power (RAPL) unavailable"). Granting it: a udev rule
  `ACTION=="add", SUBSYSTEM=="powercap", KERNEL=="intel-rapl:0", RUN+="/bin/chmod 0444 /sys%p/energy_uj"`
  (re-opens the side channel for local users; the owner decides). Intel iGPU load/temperature/power
  and NVIDIA without the proprietary driver stay null (note). VMs without hwmon: temperatures null.
- **macOS, normal user:** temperatures, fans, GPU load, battery, media work. CPU/GPU power and CPU clock
  need `powermetrics` = root (note). Media control and shutdown/restart trigger the **Automation**
  permission prompt (Privacy & Security › Automation) on first use. Apple Silicon GPUs share system
  memory: VRAM fields are null. IOHID temperature services are private-but-exported IOKit symbols; if
  Apple changes them, temperatures become null (never wrong).
- **Virtual machines / WSL:** a Hyper-V "Virtual Battery" reports the host's percentage but synthetic
  energy (5 Wh): only %, plugged and charging are sent.

## Packaging and CI

- Minimum systems follow the Qt 6.12 wheels: **macOS 14** (`macosx_14_0` universal2) and
  **glibc 2.34** on Linux (the CI build on Ubuntu 22.04 needs glibc ≥ 2.35).
- PyInstaller one-folder build everywhere (one spec): Windows `.exe` + Inno Setup (unchanged);
  macOS `PC Control Deck.app` (`DECKY Agent.app` since 1.0; `LSUIElement`: menu bar only, `NSAppleEventsUsageDescription`)
  zipped and as `.dmg`, **unsigned**; Linux `.tar.gz` with the app folder, a `.desktop` file, the icon
  and a per-user `install.sh`. No AppImage yet.
- `.github/workflows/build.yml` (this repository; `agent.yml` while the app lived in the deck
  repository): tests + builds on Windows, macOS arm64 (`macos-latest`), macOS x86_64
  (`macos-15-intel`: GitHub retired `macos-13`) and Ubuntu 22.04; releases on `v*` tags.
  The deck repositories keep `.github/workflows/firmware.yml`: PlatformIO builds, on-target tests
  compiled, merged images (since 1.0 one repository for every deck,
  [DECKY-Firmware](https://github.com/crazyramirez/DECKY-Firmware)).

## Consequences

- Linux/macOS code paths are covered by unit tests with fake sysfs trees, injected command runners and
  sample tool outputs on every CI OS; the Linux provider was also run against a real (WSL2) kernel.
  The macOS IOKit / osascript paths have not run on a real Mac yet: expect fixes after the first test.
- The tray app on GNOME needs an AppIndicator/StatusNotifier extension (Ubuntu ships one); without a
  tray, closing the window minimizes it instead of hiding it.
- Unsigned macOS builds need "Open Anyway" (Gatekeeper); signing/notarization needs an Apple
  Developer account and is out of scope.
