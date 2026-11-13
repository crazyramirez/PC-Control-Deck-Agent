# PyInstaller spec for the PC app (one-folder, windowed) on Windows, macOS and Linux.
#   Windows: scripts/build-agent.ps1 (prepares build/pcdeck.ico, build/version_info.txt and vendor/lhm-*)
#            -> dist/PCControlDeck/PCControlDeck.exe (+ Inno Setup installer)
#   macOS:   scripts/build-agent.sh (prepares build/pcdeck.png) -> dist/DECKY Agent.app (LSUIElement)
#   Linux:   scripts/build-agent.sh -> dist/PCControlDeck/PCControlDeck (packed as .tar.gz)
import os
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

AGENT = Path(SPECPATH).parent
VERSION = re.search(r'__version__ = "([^"]+)"', (AGENT / "src" / "pcdeck_agent" / "__init__.py").read_text()).group(1)
IS_WIN, IS_MAC = sys.platform == "win32", sys.platform == "darwin"
EXCLUDES = ["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore", "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtPdf", "PySide6.QtMultimedia"]

if IS_WIN:
    LHM_DIR = AGENT / "vendor" / "lhm-0.9.6"
    ICON = AGENT / "build" / "pcdeck.ico"
    VERSION_FILE = AGENT / "build" / "version_info.txt"

    # LibreHardwareMonitor (.NET Framework build) and its dependencies -> <bundle>/lhm
    lhm = [(str(p), "lhm") for p in LHM_DIR.glob("*.dll")] + [(str(p), "lhm") for p in LHM_DIR.glob("*.txt")]

    datas = lhm
    datas += collect_data_files("pythonnet")      # Python.Runtime.dll
    datas += collect_data_files("clr_loader")     # ClrLoader.dll (netfx host)
    binaries = collect_dynamic_libs("clr_loader")
    hiddenimports = ["clr", "pythonnet", "clr_loader", "zeroconf._utils.ipaddress", "zeroconf._handlers.answers",
                     "pycaw.pycaw", "comtypes.stream"] + collect_submodules("winrt")
else:
    ICON = AGENT / "build" / "pcdeck.png"  # PyInstaller turns it into .icns on macOS (Pillow)
    VERSION_FILE = None
    datas, binaries = [], []
    hiddenimports = ["zeroconf._utils.ipaddress", "zeroconf._handlers.answers",
                     # platform modules are imported lazily (platforms.py): list them explicitly
                     "pcdeck_agent.hardware.linux_provider", "pcdeck_agent.hardware.linux_battery",
                     "pcdeck_agent.commands.linux_media", "pcdeck_agent.hardware.macos_provider",
                     "pcdeck_agent.hardware.macos_battery", "pcdeck_agent.commands.macos_media",
                     "pcdeck_agent.gui.autostart_posix"]
    if not IS_MAC:
        hiddenimports += ["jeepney", "jeepney.io.blocking", "jeepney.bus_messages", "jeepney.wrappers", "pynvml"]

# USB firmware updater: esptool loads its flasher stubs (JSON) from its package folder, and the
# decks' latest firmware ships inside the app (tools/fetch_firmware.py -> build/firmware).
datas += collect_data_files("esptool") + collect_data_files("esp_pylib")
hiddenimports += ["serial.tools.list_ports"]
FIRMWARE = AGENT / "build" / "firmware"
if (FIRMWARE / "manifest.json").exists():
    datas.append((str(FIRMWARE), "firmware"))
elif os.environ.get("CI"):
    raise SystemExit("build/firmware is missing: run tools/fetch_firmware.py before PyInstaller")
else:
    print("WARNING: no bundled deck firmware (run tools/fetch_firmware.py); the updater will use GitHub")

a = Analysis(
    [str(AGENT / "packaging" / "launcher.py")],
    pathex=[str(AGENT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=EXCLUDES,
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
    version=str(VERSION_FILE) if VERSION_FILE is not None and VERSION_FILE.exists() else None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="PCControlDeck")

if IS_MAC:
    app = BUNDLE(
        coll,
        name="DECKY Agent.app",
        icon=str(ICON) if ICON.exists() else None,
        bundle_identifier="com.pcdeck.agent",
        version=VERSION,
        info_plist={
            "CFBundleName": "DECKY Agent",
            "CFBundleDisplayName": "DECKY Agent",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSUIElement": True,  # menu-bar app: no Dock icon
            "LSMinimumSystemVersion": "14.0",  # PySide6 6.12 wheels are macosx_14_0
            "NSHighResolutionCapable": True,
            "NSAppleEventsUsageDescription": "DECKY Agent controls Music and Spotify from your deck and, only if you allow it, shuts down or restarts the Mac.",
            "NSLocalNetworkUsageDescription": "DECKY Agent sends telemetry to your paired deck on the local network.",
            "NSBonjourServices": ["_pcdeck._tcp"],
        },
    )
