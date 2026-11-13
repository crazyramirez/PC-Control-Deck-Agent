# ADR 0006 — Deck firmware over USB from the desktop app

- **Status:** accepted (owner request, 2026-11-13) · app 0.8.0, deck firmware JC4827W543 0.12.2,
  JC3248W535 0.13.2, JC1060P470C 0.12.4 (`id` console command) · [updated for 1.0](#update-10)
- **Extends:** [ADR 0003](0003-pairing-tray-installer.md) (tray app), [ADR 0005](0005-cross-platform-agent.md) (three systems)

## Context

Flashing a deck meant the browser flasher or PlatformIO. The owner wants the desktop app (Windows,
macOS, Linux) to do it: detect the board on USB, pick the right image by itself, always install the
latest version. The app must also **carry that firmware inside it**, checked and working, and more
deck models will follow.

## Decision

1. **esptool as a library.** `esptool==5.6.0` (Espressif's flasher, pure Python, with pyserial)
   runs inside the app through its public API (`detect_chip`, `run_stub`, `attach_flash`,
   `erase_flash`, `write_flash`, `reset_chip`). Its console output goes to the app log through a
   custom logger, and its progress bars feed the UI. PyInstaller bundles its flasher stubs (JSON).
2. **Catalog** (`flasher/boards.py`): one `Board` per model, with:
   - key: what the deck's `id` command reports;
   - chip: esptool's `CHIP_NAME`;
   - GitHub repository;
   - variants: PlatformIO environments, each with the lowest chip major revision it runs on. The
     JC1060P470C has `deck` (P4 v1.x) and `deck_r3` (v3.x).

   Adding a model is one entry, a firmware that answers `id`, and a firmware CI that publishes
   `pcdeck-<variant>-<version>-merged.bin`.
3. **Recognising the deck without touching it** (`flasher/probe.py`):
   - Ports with Espressif's USB VID (0x303A) — and, since 1.1, the USB-UART bridges a deck may sit
     behind (CH340, CH343, CP210x: `USB_UART_BRIDGES`; the ESP32-4848S040's USB-C is a CH340) — are
     opened with DTR/RTS released, so the chip is not reset (a bridge's auto-reset circuit stays
     idle too), and asked `id` → `pcdeck board=<key> fw=<version>`.
   - Firmware from before `id` answers `status`, and the model comes from its lines:
     `touch GT911 … 1024x600` is the JC1060P470C, `480x272` the JC4827W543, and
     `screen … | touch presses=` the JC3248W535.
   - A port that does not answer is a board without the deck firmware. The user picks the model,
     and esptool rejects a model whose chip family does not match.
4. **One image per variant.** A release's merged image (from 0x0) holds the app image byte for byte
   from 0x10000; this was checked on the S3 and P4 releases.
   - **Update:** writes `merged[0x10000:]` at 0x10000. Bootloader, partition table and NVS (Wi-Fi,
     pairing, settings) stay.
   - **Clean install:** `erase_flash` + the whole merged image at 0x0. Forced for boards that do not
     answer as a deck, because their partition table is unknown.
5. **The firmware ships inside the app.** `tools/fetch_firmware.py` runs before PyInstaller (build
   scripts and CI, with the CI token for the API limit only).
   - It downloads the latest release of every model into `build/firmware/<model>/`, with
     `manifest.json`, and the spec bundles it as `firmware/`.
   - A release exists only after its firmware CI build passed. Every image must also match GitHub's
     SHA-256 digest and pass `flasher/images.py`: partition table magic, bootloader and app ESP
     headers for the board's chip id, checksum and appended SHA-256.
   - A missing model or variant, or a bad image, fails the build: no app release without them.
   - Files are kept per model: the S3 decks share image names.
6. **Latest at run time.** The dialog starts with the bundled release (offline, instant). If
   GitHub's latest release of a model is newer, it is downloaded to `<app_dir>/firmware/<model>/`,
   verified and used. Before writing, the image is checked again (`images.check`) and against the
   connected chip.
7. **UI** (`gui/flash_dialog.py`, *Paired devices › Update firmware over USB…*):
   - One card per detected port: model, port, installed and latest version, *Clean install*, and a
     button *Update to / Reinstall / Install X*.
   - Progress through the stages connecting, downloading, preparing, erasing, writing and
     restarting.
   - The dialog cannot close mid-flash. Ports are scanned every 3 s on worker threads.

## Consequences

- The app grows by about 12 MB of firmware plus esptool and its dependencies (rich, cryptography).
- A newer firmware released after an app version reaches users through the run-time GitHub check;
  the bundle catches up with the next app release. Release order: firmware first, then the app.
- The app talks to two more places, only when the updater dialog is open: GitHub's API and release
  downloads. It sends no identifiers, only a generic User-Agent.
- Flashing the wrong S3 model, which only a board without the deck firmware allows, leaves a dark
  screen until the right one is installed. The dialog asks for confirmation first.

## Update (1.0)

The three deck firmware repositories were merged into one,
[DECKY-Firmware](https://github.com/crazyramirez/DECKY-Firmware): one version for every deck, one
release with every deck's images. Every `Board` now reads that repository (`boards.FIRMWARE_REPO`),
and the variants are named after the deck: `JC1060P470C` / `JC1060P470C_r3` (P4 v1.x / v3.x),
`JC4827W543` and `JC3248W535`. The images are `pcdeck-<DECK>-<version>.bin` /
`-merged.bin`, so the S3 decks no longer share image names (files are still kept per model); the
run-time cache drops images with the old names (`pcdeck-deck-…`, `pcdeck-deck_r3-…`). The decisions
above are otherwise unchanged.
