"""Self-update of the desktop app from its GitHub releases (Windows, macOS, Linux).

``check()`` finds the latest release of this repository when it is newer than the running app,
with the build for this system. ``fetch()`` downloads it, verified against its size and GitHub's
SHA-256 digest. ``apply()`` hands over to whatever replaces the app; the caller quits right after,
and the new version is started for it:

- Windows (installed with the setup): the setup itself, silently (``/VERYSILENT``), keeping the
  previous tasks and options. ``/AUTOUPDATE=1`` makes it start the app again. The setup is opened
  with ShellExecute "runas": no prompt when the app already runs elevated (it does when started by
  Windows or from its Start-menu entry, through the scheduled task), a UAC prompt otherwise.
- macOS: the ``.app`` from the release zip (unpacked with ``ditto``, which keeps symlinks and
  permissions) replaces the running bundle, then ``open`` starts it.
- Linux: the ``PCControlDeck`` folder from the release tarball replaces the running one, then it is
  started again.

On macOS and Linux a small ``sh`` script does the swap once this process has exited. Builds that
cannot replace themselves (the Windows portable zip, a read-only location, running from source)
only report the update.
"""

from __future__ import annotations

import logging
import os
import platform
import shlex
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePath

from . import __version__
from .config import app_dir
from .flasher.boards import OWNER, is_newer
from .flasher.releases import Asset, Release, latest_of
from .flasher.releases import download as download_asset

REPO = "PC-Control-Deck-Agent"
RELEASES_URL = f"https://github.com/{OWNER}/{REPO}/releases/latest"
RETRY_FAILED_AFTER_S = 24 * 3600

log = logging.getLogger("pcdeck_agent.updater")


class UpdateError(Exception):
    """A failure with a UI string key (``gui/strings.py``) and its parameters."""

    def __init__(self, key: str, **params: object) -> None:
        super().__init__(key, params)
        self.key = key
        self.params = params


@dataclass(frozen=True)
class Update:
    version: str
    asset: Asset | None  # this system's build; None when this install cannot update itself


def machine() -> str:
    m = platform.machine().lower()
    return {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(m, m)


def bundle_of(exe: Path) -> Path | None:
    """The ``.app`` bundle an executable lives in (macOS)."""
    return next((p for p in exe.parents if p.suffix == ".app"), None)


def install_kind(exe: Path | None = None, frozen: bool | None = None, platform_name: str | None = None) -> str | None:
    """"windows" / "macos" / "linux" when this install can replace itself, else None."""
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    platform_name = platform_name or sys.platform
    if not frozen:
        return None  # running from source
    exe = (exe or Path(sys.executable)).resolve()
    if platform_name == "win32":
        # The setup puts setup-tasks.ps1 next to the exe; the portable zip has no setup to run.
        return "windows" if (exe.parent / "setup-tasks.ps1").exists() else None
    if platform_name == "darwin":
        return "macos" if bundle_of(exe) else None
    return "linux"


def asset_matches(name: str, kind: str, arch: str) -> bool:
    if kind == "windows":
        return name.startswith("PCControlDeck-Setup-") and name.endswith(".exe")
    if kind == "macos":
        return name.endswith(f"-macos-{arch}.zip")
    if kind == "linux":
        return name.endswith(f"-linux-{arch}.tar.gz")
    return False


def pick(release: Release, current: str, kind: str | None, arch: str) -> Update | None:
    """The update ``release`` offers over ``current`` (None: nothing newer)."""
    if not is_newer(release.version, current):
        return None
    asset = None
    if kind is not None:
        asset = next((a for name, a in sorted(release.assets.items()) if asset_matches(name, kind, arch)), None)
    return Update(release.version, asset)


def check(current: str = __version__) -> Update | None:
    """Raises flasher.releases.ReleaseError when GitHub cannot be reached.

    Testing aid: ``PCDECK_UPDATE_FROM=0.0.1`` makes the app behave as that version, so an installed
    build updates itself to the published release (the whole path, without a newer release)."""
    pretend = os.environ.get("PCDECK_UPDATE_FROM")
    if pretend:
        log.warning("PCDECK_UPDATE_FROM=%s: checking as if this were version %s", pretend, pretend)
        current = pretend
    return pick(latest_of(REPO), current, install_kind(), machine())


def can_replace(kind: str | None, exe: Path | None = None) -> bool:
    """The running app's location is writable for the swap (Windows: the setup elevates)."""
    if kind == "windows":
        return True
    exe = (exe or Path(sys.executable)).resolve()
    if kind == "macos":
        app = bundle_of(exe)
        return app is not None and os.access(app.parent, os.W_OK) and os.access(app, os.W_OK)
    if kind == "linux":
        return os.access(exe.parent.parent, os.W_OK) and os.access(exe.parent, os.W_OK)
    return False


def fetch(update: Update, progress: Callable[[int, int], None] | None = None) -> Path:
    if update.asset is None:
        raise UpdateError("update_manual", v=update.version)
    dest = app_dir() / "updates"
    if dest.is_dir():
        for old in dest.iterdir():
            if old.is_file() and old.name != update.asset.name:
                old.unlink(missing_ok=True)
    return download_asset(update.asset, dest, progress)


def swap_script(old: PurePath, new: PurePath, workdir: PurePath, relaunch: list[str], pid: int) -> str:
    """sh script: wait for ``pid`` to exit, put ``new`` in place of ``old`` (rolling back if the
    move fails), clean up, start the app again."""
    q = shlex.quote
    bak = q(str(old) + ".old-update")
    return f"""#!/bin/sh
# Control Deck self-update: replace the app once it has quit, then start the new one.
i=0
while kill -0 {pid} 2>/dev/null && [ $i -lt 120 ]; do sleep 0.5; i=$((i+1)); done
rm -rf {bak}
if mv {q(str(old))} {bak} && mv {q(str(new))} {q(str(old))}; then
    rm -rf {bak}
else
    [ -e {q(str(old))} ] || mv {bak} {q(str(old))}
fi
xattr -dr com.apple.quarantine {q(str(old))} 2>/dev/null || true
rm -rf {q(str(workdir))}
exec {" ".join(q(a) for a in relaunch)} >/dev/null 2>&1
"""


def _spawn(script: str, workdir: Path) -> None:
    path = workdir / "swap.sh"
    path.write_text(script, encoding="utf-8")
    subprocess.Popen(  # noqa: S603 (our own script, fixed interpreter)
        ["/bin/sh", str(path)], start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip


def apply(path: Path, kind: str | None) -> None:
    """Start the replacement. The caller quits the app as soon as this returns."""
    if kind == "windows":
        _run_setup(path)
        return
    if kind not in ("macos", "linux") or not can_replace(kind):
        raise UpdateError("update_manual_location")
    exe = Path(sys.executable).resolve()
    workdir = Path(tempfile.mkdtemp(prefix="pcdeck-update-"))
    if kind == "macos":
        r = subprocess.run(["/usr/bin/ditto", "-x", "-k", str(path), str(workdir)], capture_output=True, text=True)
        new = next(workdir.glob("*.app"), None)
        old = bundle_of(exe)
        if r.returncode != 0 or new is None or old is None:
            raise UpdateError("update_failed", msg=r.stderr.strip() or "no .app in the archive")
        relaunch = ["/usr/bin/open", str(old), "--args", "--minimized"]
    else:
        with tarfile.open(path) as t:
            t.extractall(workdir, filter="data")
        new = next(workdir.glob("*/PCControlDeck"), None)
        old = exe.parent
        if new is None or not (new / "PCControlDeck").exists():
            raise UpdateError("update_failed", msg="no PCControlDeck folder in the archive")
        relaunch = [str(old / "PCControlDeck"), "--minimized"]
    _spawn(swap_script(old, new, workdir, relaunch, os.getpid()), workdir)
    log.info("update: %s -> %s, swap script started", new, old)


def _run_setup(path: Path) -> None:
    import ctypes

    params = "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /AUTOUPDATE=1"
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", str(path), params, None, 0)
    if rc <= 32:  # 5: the UAC prompt was declined
        raise UpdateError("update_denied" if rc == 5 else "update_failed", msg=f"ShellExecute {rc}")
    log.info("update: setup %s started", path.name)


def recently_failed(version: str, attempt_version: str, attempt_at: float, now: float) -> bool:
    """An automatic attempt at ``version`` was made recently and we are still not on it."""
    return attempt_version == version and now - attempt_at < RETRY_FAILED_AFTER_S
