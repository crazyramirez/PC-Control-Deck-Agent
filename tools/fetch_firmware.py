"""Bundle the decks' firmware into the app: the latest release of every model, merged images of
every variant, into build/firmware/<model>/ with a manifest.json (read by flasher/bundled.py).

Run before PyInstaller (scripts/build-agent.ps1 / .sh, CI). The images are the ones each firmware
repository's CI built and published for its release (a release exists only if that build passed),
checked against GitHub's SHA-256 digests and as ESP images for the board's chip (flasher/images.py).
Any missing variant or unsound image fails the build: an app version never ships without them.

Usage: python tools/fetch_firmware.py [--out build/firmware]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

AGENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT / "src"))

from pcdeck_agent.flasher import boards, bundled, images, releases  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=AGENT / "build" / "firmware")
    out: Path = ap.parse_args().out
    if out.exists():
        shutil.rmtree(out)
    manifest: dict = {"boards": {}}
    for b in boards.BOARDS:
        rel = releases.latest(b, token=os.environ.get("GITHUB_TOKEN"))
        entries = []
        # Every variant the catalog knows must be in the release, and every image must be sound
        # for the board's chip: otherwise this app version must not ship.
        for env, _rev in b.variants:
            name = b.image(rel.version, env)
            a = rel.assets.get(name)
            if a is None:
                print(f"error: {b.repo} {rel.tag} has no {name}", file=sys.stderr)
                return 1
            path = releases.download(a, out / b.key)
            try:
                images.check(path.read_bytes(), b)
            except images.ImageError as e:
                print(f"error: {b.repo} {rel.tag} {name}: {e}", file=sys.stderr)
                return 1
            entries.append({"name": a.name, "size": a.size, "sha256": a.sha256 or releases.sha256_of(path)})
            print(f"{b.key:12} {rel.tag:8} {a.name:36} {a.size:>9} bytes  image ok  sha256 {'(GitHub)' if a.sha256 else '(computed)'}")
        manifest["boards"][b.key] = {"tag": rel.tag, "assets": entries}
    (out / bundled.MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"firmware bundle: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
