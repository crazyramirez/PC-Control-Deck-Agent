"""Render the application icon to an .ico for PyInstaller and Inno Setup."""

import sys

from PySide6.QtGui import QGuiApplication

from pcdeck_agent.gui.icons import save_ico

if __name__ == "__main__":
    out = sys.argv[1]
    app = QGuiApplication(sys.argv[:1])
    if not save_ico(out):
        raise SystemExit(f"could not write {out}")
    print(f"wrote {out}")
