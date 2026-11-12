# PyInstaller spec for PCControlDeck.exe (one-folder, windowed).
# Build with scripts/build-agent.ps1 (it prepares build/pcdeck.ico and vendor/lhm-*).
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

AGENT = Path(SPECPATH).parent
LHM_DIR = AGENT / "vendor" / "lhm-0.9.6"
ICON = AGENT / "build" / "pcdeck.ico"
VERSION_FILE = AGENT / "build" / "version_info.txt"

# LibreHardwareMonitor (.NET Framework build) and its dependencies -> <bundle>/lhm
lhm = [(str(p), "lhm") for p in LHM_DIR.glob("*.dll")] + [(str(p), "lhm") for p in LHM_DIR.glob("*.txt")]

datas = lhm
datas += collect_data_files("pythonnet")      # Python.Runtime.dll
datas += collect_data_files("clr_loader")     # ClrLoader.dll (netfx host)
binaries = collect_dynamic_libs("clr_loader")

a = Analysis(
    [str(AGENT / "packaging" / "launcher.py")],
    pathex=[str(AGENT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=["clr", "pythonnet", "clr_loader", "zeroconf._utils.ipaddress", "zeroconf._handlers.answers",
                   "pycaw.pycaw", "comtypes.stream"] + collect_submodules("winrt"),
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore", "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtPdf", "PySide6.QtMultimedia"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PCControlDeck",
    console=False,
    icon=str(ICON) if ICON.exists() else None,
    version=str(VERSION_FILE) if VERSION_FILE.exists() else None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="PCControlDeck")
