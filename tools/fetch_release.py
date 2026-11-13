"""Download the latest published release of this app, every system's build, into
dist/release/<system>/ so it can be installed from this machine.

The Linux and macOS builds cannot be made on Windows (PyInstaller does not cross-compile): CI builds
each on its own system and publishes them; this fetches what was published, checks every file
against GitHub's SHA-256 digest, and removes every older version from dist/ (release folders,
dist/installer, the local dist/PCControlDeck build).

Usage: python tools/fetch_release.py
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

AGENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT / "src"))

from pcdeck_agent.flasher import releases  # noqa: E402

REPO = "DECKY-Agent"
SYSTEMS = ("windows-x64", "linux-x86_64", "macos-arm64", "macos-x86_64")


def system_of(name: str) -> str | None:
    if "-Setup-" in name or "windows" in name:
        return "windows-x64"
    return next((s for s in SYSTEMS if s in name), None)


def main() -> int:
    rel = releases.latest_of(REPO, token=os.environ.get("GITHUB_TOKEN"))
    out = AGENT / "dist" / "release"
    wanted: dict[str, set[str]] = {s: set() for s in SYSTEMS}
    for name, asset in sorted(rel.assets.items()):
        system = system_of(name)
        if system is None:
            print(f"skipped {name}")
            continue
        path = releases.download(asset, out / system)
        wanted[system].add(path.name)
        print(f"{system:13} {name:52} {asset.size / 1e6:6.1f} MB  {'sha256 ok' if asset.sha256 else 'size ok'}")
    for system, names in wanted.items():
        if not names:
            print(f"error: release {rel.tag} has no {system} build", file=sys.stderr)
            return 1
        for old in (out / system).iterdir():
            if old.is_file() and old.name not in names:
                old.unlink()
                print(f"removed {system}/{old.name}")
    # Only the latest version stays in dist/: older local build outputs go too (the release's
    # Windows installer and portable zip replace dist/installer and dist/PCControlDeck).
    dist = AGENT / "dist"
    for old in (dist / "installer").glob("*") if (dist / "installer").is_dir() else ():
        if old.is_file() and rel.version not in old.name:
            old.unlink()
            print(f"removed installer/{old.name}")
    local_build = dist / "PCControlDeck"
    if local_build.is_dir():
        shutil.rmtree(local_build)
        print("removed PCControlDeck/ (local build; the portable zip has the same app)")
    print(f"{rel.tag} for every system in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
