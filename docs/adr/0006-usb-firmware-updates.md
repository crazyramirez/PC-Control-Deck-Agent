# ADR 0006 — Deck firmware over USB from the desktop app

- **Status:** accepted (owner request, 2026-11-13) · app 0.8.0, deck firmware JC4827W543 0.12.2,
  JC3248W535 0.13.2, JC1060P470C 0.12.4 (`id` console command)
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
   - Ports with Espressif's USB VID (0x303A) are opened with DTR/RTS released, so the chip is not
     reset, and asked `id` → `pcdeck board=<key> fw=<version>`.
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
