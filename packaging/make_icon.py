"""Render the application icon: .ico (Windows: PyInstaller + Inno Setup) or .png (macOS / Linux)."""

import os
import sys

if sys.platform != "win32":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # CI runners have no display

from PySide6.QtGui import QGuiApplication

from pcdeck_agent.gui.icons import save_ico, save_png

if __name__ == "__main__":
    out = sys.argv[1]
    app = QGuiApplication(sys.argv[:1])
    ok = save_png(out) if out.lower().endswith(".png") else save_ico(out)
    if not ok:
        raise SystemExit(f"could not write {out}")
    print(f"wrote {out}")
