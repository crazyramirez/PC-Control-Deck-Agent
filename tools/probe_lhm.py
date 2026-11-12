"""Diagnostic: load LibreHardwareMonitorLib through pythonnet and list every sensor.

Prints hardware type, name, identifier, sensor type, value and unit. No secrets involved.
Usage:  .venv\\Scripts\\python tools\\probe_lhm.py
"""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pcdeck_agent.hardware.lhm_loader import load_lhm  # noqa: E402


def main() -> int:
    is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    print(f"admin={is_admin}")
    lhm = load_lhm()
    computer = lhm.open_computer()
    try:
        time.sleep(1.0)  # load-type sensors need two samples
        for hw in lhm.iter_hardware(computer):
            hw.Update()
        for hw in lhm.iter_hardware(computer):
            print(f"\n[{hw.HardwareType}] {hw.Name}  ({hw.Identifier})")
            for s in hw.Sensors:
                v = s.Value
                vs = "null" if v is None else f"{float(v):.2f}"
                print(f"   {str(s.SensorType):<12} {s.Name:<34} {vs:>10}   {s.Identifier}")
    finally:
        computer.Close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
