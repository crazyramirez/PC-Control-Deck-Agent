"""Firmware shipped inside the app: each model's latest release at build time.

``tools/fetch_firmware.py`` writes ``build/firmware/<model>/<merged image>`` plus ``manifest.json``
before PyInstaller runs; the spec bundles that folder as ``firmware``. From source, without a
fetched bundle, there is simply nothing bundled and the updater uses GitHub.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from .boards import parse_version
from .releases import Asset, Release

log = logging.getLogger("pcdeck.flasher")

MANIFEST = "manifest.json"


def bundle_dir() -> Path:
    if getattr(sys, "frozen", False):  # PyInstaller bundle
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "firmware"
    return Path(__file__).resolve().parents[3] / "build" / "firmware"  # source checkout


def load(root: Path | None = None) -> dict[str, Release]:
    """Bundled release per model key ({} when nothing is bundled or the manifest is unreadable)."""
    root = root or bundle_dir()
    try:
        data = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, Release] = {}
    for key, entry in (data.get("boards") or {}).items():
        tag = str(entry.get("tag") or "")
        if parse_version(tag) is None:
            log.warning("bundled firmware %s: bad tag %r", key, tag)
            continue
        assets = {}
        for a in entry.get("assets") or []:
            name = str(a.get("name") or "")
            if not name or "/" in name or "\\" in name:
                continue
            assets[name] = Asset(name, "", int(a.get("size") or 0), a.get("sha256"), root / key / name)
        out[key] = Release(tag, tag.lstrip("v"), assets, source="bundled")
    return out
