"""Load LibreHardwareMonitorLib (.NET Framework build) into the Python process via pythonnet.

Windows only. Uses the in-box .NET Framework 4.8 runtime ("netfx"), so no .NET SDK is needed.
DLLs come from agent/vendor/lhm-<version>/ (see tools/fetch_lhm.py) or PCDECK_LHM_DIR.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

LHM_VERSION = "0.9.6"
_lock = threading.Lock()
_loaded: "LhmApi | None" = None


class LhmUnavailable(RuntimeError):
    """LibreHardwareMonitor cannot be used on this machine (missing DLLs, non-Windows, ...)."""


def default_lhm_dir() -> Path:
    env = os.environ.get("PCDECK_LHM_DIR")
    if env:
        return Path(env)
    import sys

    if getattr(sys, "frozen", False):  # PyInstaller bundle: DLLs shipped in <bundle>/lhm
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "lhm"
    return Path(__file__).resolve().parents[3] / "vendor" / f"lhm-{LHM_VERSION}"


@dataclass
class LhmApi:
    Computer: Any
    HardwareType: Any
    SensorType: Any
    dll_dir: Path

    def open_computer(self) -> Any:
        c = self.Computer()
        c.IsCpuEnabled = True
        c.IsGpuEnabled = True
        c.IsMemoryEnabled = True
        c.IsMotherboardEnabled = True
        c.IsStorageEnabled = True
        c.IsControllerEnabled = True
        c.IsNetworkEnabled = False  # network throughput comes from psutil
        c.Open()
        return c

    @staticmethod
    def iter_hardware(computer: Any) -> Iterator[Any]:
        """Yield every hardware node, including sub-hardware (e.g. Super I/O under the board)."""
        for hw in computer.Hardware:
            yield hw
            for sub in hw.SubHardware:
                yield sub


def load_lhm(dll_dir: Path | None = None) -> LhmApi:
    global _loaded
    with _lock:
        if _loaded is not None:
            return _loaded
        if os.name != "nt":
            raise LhmUnavailable("LibreHardwareMonitor is Windows-only")
        d = Path(dll_dir) if dll_dir else default_lhm_dir()
        lib = d / "LibreHardwareMonitorLib.dll"
        if not lib.exists():
            raise LhmUnavailable(f"{lib} not found - run tools/fetch_lhm.py")

        from pythonnet import load  # imported lazily: only on Windows

        try:
            load("netfx")
        except RuntimeError:
            pass  # runtime already loaded in this process
        import clr  # type: ignore[import-not-found]
        import System  # type: ignore[import-not-found]

        # LHM's app.config carries binding redirects we don't have in python.exe; resolve any
        # dependency by simple name from the vendor directory instead.
        def _resolve(_sender: Any, args: Any) -> Any:
            name = System.Reflection.AssemblyName(args.Name).Name
            candidate = d / f"{name}.dll"
            if candidate.exists():
                return System.Reflection.Assembly.LoadFrom(str(candidate))
            return None

        System.AppDomain.CurrentDomain.AssemblyResolve += System.ResolveEventHandler(_resolve)
        clr.AddReference(str(lib))
        from LibreHardwareMonitor.Hardware import (  # type: ignore[import-not-found]
            Computer,
            HardwareType,
            SensorType,
        )

        _loaded = LhmApi(Computer=Computer, HardwareType=HardwareType, SensorType=SensorType, dll_dir=d)
        return _loaded
