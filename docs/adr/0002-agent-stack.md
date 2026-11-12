# ADR 0002 — Agent stack: Python 3.13 + LibreHardwareMonitor via pythonnet (deviation from spec)

- **Status:** accepted (owner decision, 2026-11-11)
- **Spec reference:** §4 asks for C# / .NET 8 with LibreHardwareMonitorLib.

## Context

- .NET 8 reached end of support on 2026-11-10; the current LTS is .NET 10. Neither SDK is installed
  on the development PC (only the .NET 8.0.0 runtime).
- The owner wants a future **macOS** agent. LibreHardwareMonitor is Windows-only in any language,
  so portability depends on isolating the sensor provider, not on the language.
- Python 3.13.5 is already installed; Node/Electron was rejected (no sensor library, ~150 MB RAM idle).

## Decision

- Agent in **Python 3.13** (`agent/`), exact dependency pins in `pyproject.toml`:
  `pythonnet 3.2.1`, `psutil 7.2.2`, `websockets 17.4`, `pytest 9.2.0` (dev).
- Windows sensors: **LibreHardwareMonitorLib 0.9.6** (MPL-2.0), .NET Framework 4.7.2 build from the
  official release zip (SHA-256 pinned in `tools/fetch_lhm.py`), loaded with pythonnet on the in-box
  **.NET Framework 4.8.1** runtime (`netfx`). No .NET SDK needed. DLLs are fetched, never committed.
- Dependency binding redirects are replaced by an `AssemblyResolve` handler pointing at the vendor dir.
- Platform split: `HardwareMetricsProvider` protocol → `LhmProvider` (Windows), `FakeHardwareMetricsProvider`
  (tests / `--fake`), macOS provider later (IOKit/IOReport/SMC). Protocol, pairing, commands and
  normalization are platform-independent.
- RAM comes from the OS (`psutil`), not from sensors.

## Verified on the dev PC (Lenovo Legion, Core Ultra 9 275HX, RTX 5070 Ti Laptop + Intel iGPU), not elevated

| Metric | Result |
|---|---|
| CPU load | ✅ |
| CPU temperatures / clocks | ❌ null (no MSR access) |
| CPU package power | ❌ LHM reports 0.0 W → normalized to **null** |
| GPU (NVIDIA) temp / load / VRAM / package power | ✅ |
| Intel iGPU | ✅ detected, not selected (discrete preferred) |
| RAM (OS) | ✅ |
| Storage temps, fans | ❌ not exposed (not elevated; laptop fans are EC-controlled) |

161 sensors: 82 ok, 75 missing, 4 invalid.

## Consequences

- Full CPU thermals need **administrator** rights **and** the **PawnIO** driver (LHM ≥ 0.9.5 uses it
  instead of WinRing0). Installing a kernel driver is a system change: it will only be proposed to
  the user with explicit consent; the agent never elevates itself (spec §4).
- Distribution (Hito 5): PyInstaller or similar; antivirus false positives must be tested.
- macOS sensor APIs on Apple Silicon are private and may change between OS releases.
