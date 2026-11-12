"""Download the pinned LibreHardwareMonitor release and extract its .NET Framework DLLs.

The binaries are NOT committed. They land in vendor/lhm-<version>/ (git-ignored).
The SHA-256 of the release zip is pinned; a mismatch aborts.

LibreHardwareMonitor is MPL-2.0: https://github.com/LibreHardwareMonitor/LibreHardwareMonitor

Usage:  .venv\\Scripts\\python tools\\fetch_lhm.py
"""

from __future__ import annotations

import hashlib
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

LHM_VERSION = "0.9.6"
LHM_URL = (
    "https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/releases/download/"
    f"v{LHM_VERSION}/LibreHardwareMonitor.zip"
)
LHM_SHA256 = "086D9F1B5A99E643EDC2CFAAAC16051685B551E4C5AC0B32A57C58C0E529C001"

AGENT_DIR = Path(__file__).resolve().parents[1]
DEST = AGENT_DIR / "vendor" / f"lhm-{LHM_VERSION}"


def main() -> int:
    if (DEST / "LibreHardwareMonitorLib.dll").exists():
        print(f"already present: {DEST}")
        return 0
    print(f"downloading {LHM_URL}")
    with urllib.request.urlopen(LHM_URL, timeout=60) as r:  # noqa: S310 (pinned https URL)
        data = r.read()
    digest = hashlib.sha256(data).hexdigest().upper()
    if digest != LHM_SHA256:
        print(f"SHA-256 mismatch: got {digest}, expected {LHM_SHA256}", file=sys.stderr)
        return 1
    DEST.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            p = Path(name)
            # Only top-level DLLs + license; skip the GUI exe and anything nested.
            if len(p.parts) == 1 and p.suffix.lower() in {".dll", ".txt"}:
                (DEST / p.name).write_bytes(z.read(name))
    print(f"extracted to {DEST}")
    for f in sorted(DEST.iterdir()):
        print(f"  {f.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
