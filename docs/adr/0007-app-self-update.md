# ADR 0007 — The desktop app updates itself

- **Status:** accepted (owner request, 2026-11-13) · app 0.9.0
- **Extends:** [ADR 0003](0003-pairing-tray-installer.md) (installer, tasks), [ADR 0005](0005-cross-platform-agent.md) (three systems), [ADR 0006](0006-usb-firmware-updates.md) (release lookups, verified downloads)

## Context

Every release is published on GitHub by CI, one build per system. Users had to notice a new version
and install it by hand. The owner wants the app to detect a newer release and update itself.

## Decision

1. **When:** a minute after start (network up at logon) and every 6 hours, on worker threads
   (`pcdeck_agent/updater.py`, `gui/update_ctl.py`). Same release lookup and verified download
   as the deck firmware (`flasher/releases.py`: `latest_of`, size + GitHub SHA-256).
2. **What:** the latest release of this repository, when newer than the running version, with the
   asset for this install:
   - `PCControlDeck-Setup-<v>.exe` (Windows, installed with the setup);
   - `-macos-<arm64|x86_64>.zip`;
   - `-linux-<arch>.tar.gz`.

   The Windows portable zip (no `setup-tasks.ps1` next to the exe), a read-only location, or running
   from source can only report the version and open the downloads page.
3. **How:**
   - **Windows:** ShellExecute "runas" on the setup with `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART
     /AUTOUPDATE=1`.
     - No prompt when the app already runs elevated (its scheduled task starts it that way), a UAC
       prompt otherwise.
     - The setup stops the app (`taskkill` in `PrepareToInstall`) and keeps the previous tasks
       (Inno's `UsePreviousTasks`).
     - With `/AUTOUPDATE=1` it starts the new app as the signed-in user (`runasoriginaluser`); the
       app then hands itself to its elevated scheduled task, as on any start.
   - **macOS / Linux:** the archive is unpacked into a temporary folder (`ditto -x -k`; `tarfile`
     with the `data` filter). A detached `sh` script then:
     1. waits for the app's PID to exit (60 s at most);
     2. moves the old `.app` / `PCControlDeck` folder aside and the new one into its place, rolling
        back if the move fails;
     3. removes the quarantine attribute (macOS) and the temporary folder;
     4. starts the app (`open` on macOS; the executable on Linux) with `--minimized`.

   The app quits 1.5 s after handing over.
4. **Safety:**
   - No update while a deck is being flashed over USB (retried after 10 minutes).
   - The attempted version and its time go to the config: if the app starts again still older, that
     version is not tried automatically for 24 h (no update loop); the button still works.
   - Only newer versions, only this repository's releases, only verified downloads.
5. **UI:** in *This PC*, *Update the app automatically* (on by default), a status line (*up to
   date* / *available* / *downloading N %* / *installing*) and one button (*Check for updates*,
   *Update to X*, *Open downloads*). The tray shows *updated to X* on the first start of a new
   version (`last_version` in the config).

## Consequences

- An app release reaches running installs within about 6 hours, or at their next start.
- macOS and Linux need the app's folder to be writable by the user, true for `/Applications` with
  an admin account and for `~/.local/opt/pcdeck`. Otherwise the app asks for a manual download.
- The unsigned macOS app keeps launching after a swap: the files we download carry no quarantine
  attribute, and the script removes it anyway.
- Testing without a newer release: `PCDECK_UPDATE_FROM=0.0.1` makes an installed build update itself
  to the published one.
