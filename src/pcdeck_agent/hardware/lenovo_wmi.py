"""Lenovo Legion embedded-controller sensors over WMI (root\\WMI). Requires administrator rights.

Two generations are handled:
- Legacy GameZone: ``LENOVO_GAMEZONE_DATA.GetCPUTemp()`` -> Data (°C). Recent models may return 0.
- Capability interface (Legion 2023+): ``LENOVO_OTHER_METHOD.GetFeatureValue(IDs)`` -> value, with
  sensor IDs only used when the machine lists them in ``LENOVO_CAPABILITY_DATA_00``:
    0x05040000 CPU temperature (°C)     0x05050000 GPU temperature (°C)
    0x04030001 CPU fan speed (RPM)      0x04030002 GPU fan speed (RPM)
  (IDs as used by community Legion tools; validated at runtime: implausible values are dropped.)
At startup the raw values of every source are logged once so the mapping can be verified.
No kernel driver is involved.
"""

from __future__ import annotations

import logging
from typing import Any

from .models import HardwareKind, RawSensor, SensorKind

log = logging.getLogger(__name__)

ID_CPU_TEMP = 0x05040000
ID_GPU_TEMP = 0x05050000
ID_CPU_FAN = 0x04030001
ID_GPU_FAN = 0x04030002

HW_ID = "/lenovo-gamezone"
HW_NAME = "Lenovo EC"


def _plausible_temp(v: float | None) -> float | None:
    return v if v is not None and 5.0 <= v <= 120.0 else None


def _plausible_rpm(v: float | None) -> float | None:
    return v if v is not None and 0.0 <= v <= 12000.0 else None


class LenovoGameZone:
    def __init__(self) -> None:
        self._gz: Any = None  # LENOVO_GAMEZONE_DATA instance
        self._other: Any = None  # LENOVO_OTHER_METHOD instance
        self._ids: set[int] = set()  # capability IDs listed by the firmware
        self.available = False
        self.reason = ""
        self._legacy_cpu_ok = False

    # ---- discovery ----------------------------------------------------------------------------------
    def open(self) -> None:
        """Must run after pythonnet (netfx) has been loaded by lhm_loader, from an elevated process."""
        try:
            import clr  # type: ignore[import-not-found]

            clr.AddReference("System.Management")
            from System.Management import ManagementObjectSearcher  # type: ignore[import-not-found]

            def first(cls: str) -> Any:
                for obj in ManagementObjectSearcher("root\\WMI", f"SELECT * FROM {cls}").Get():
                    return obj
                return None

            self._gz = first("LENOVO_GAMEZONE_DATA")
            self._other = first("LENOVO_OTHER_METHOD")
            try:
                for cap in ManagementObjectSearcher("root\\WMI", "SELECT IDs FROM LENOVO_CAPABILITY_DATA_00").Get():
                    self._ids.add(int(cap["IDs"]))
            except Exception:
                pass
        except Exception as e:
            self.reason = type(e).__name__
            log.info("Lenovo WMI not usable: %s", self.reason)
            return
        if self._gz is None and self._other is None:
            self.reason = "no Lenovo WMI classes"
            return

        raw = self._probe_raw()
        log.info("Lenovo EC raw values: %s", ", ".join(f"{k}={v}" for k, v in raw.items()))
        self._legacy_cpu_ok = _plausible_temp(raw.get("GetCPUTemp")) is not None
        self.available = any(v is not None for v in self._read_values().values())  # 0 RPM is a real value
        if not self.available:
            self.reason = "no plausible EC sensor values"
        log.info("Lenovo EC sensors %s", "available" if self.available else f"unavailable ({self.reason})")

    def _gz_call(self, method: str) -> float | None:
        out = self._gz.InvokeMethod(method, None, None)
        v = out["Data"] if out is not None else None
        return None if v is None else float(int(v))

    def _feature(self, cap_id: int) -> float | None:
        import System  # type: ignore[import-not-found]

        params = self._other.GetMethodParameters("GetFeatureValue")
        params["IDs"] = System.UInt32(cap_id)
        out = self._other.InvokeMethod("GetFeatureValue", params, None)
        v = out["value"] if out is not None else None
        return None if v is None else float(int(v))

    def _safe(self, fn, *a) -> float | None | str:
        try:
            return fn(*a)
        except Exception as e:  # access denied / unsupported id
            return f"!{type(e).__name__}"

    def _probe_raw(self) -> dict[str, Any]:
        raw: dict[str, Any] = {}
        if self._gz is not None:
            raw["GetCPUTemp"] = self._safe(self._gz_call, "GetCPUTemp")
            raw["GetGPUTemp"] = self._safe(self._gz_call, "GetGPUTemp")
        if self._other is not None:
            for name, cid in (("cpuTemp", ID_CPU_TEMP), ("gpuTemp", ID_GPU_TEMP), ("cpuFan", ID_CPU_FAN), ("gpuFan", ID_GPU_FAN)):
                listed = cid in self._ids
                raw[f"0x{cid:08X}({name}{'' if listed else ',unlisted'})"] = self._safe(self._feature, cid) if listed else "-"
        return raw

    # ---- reading --------------------------------------------------------------------------------------
    def _num(self, v: Any) -> float | None:
        return v if isinstance(v, float) else None

    def _read_values(self) -> dict[str, float | None]:
        vals: dict[str, float | None] = {"cpu_temp": None, "cpu_fan": None, "gpu_fan": None}
        if self._other is not None:
            if ID_CPU_TEMP in self._ids:
                vals["cpu_temp"] = _plausible_temp(self._num(self._safe(self._feature, ID_CPU_TEMP)))
            if ID_CPU_FAN in self._ids:
                vals["cpu_fan"] = _plausible_rpm(self._num(self._safe(self._feature, ID_CPU_FAN)))
            if ID_GPU_FAN in self._ids:
                vals["gpu_fan"] = _plausible_rpm(self._num(self._safe(self._feature, ID_GPU_FAN)))
        if vals["cpu_temp"] is None and self._gz is not None:
            vals["cpu_temp"] = _plausible_temp(self._num(self._safe(self._gz_call, "GetCPUTemp")))
        return vals

    def read(self) -> list[RawSensor]:
        if not self.available:
            return []
        v = self._read_values()
        out = [
            RawSensor("/lenovo-gamezone/cpu/temperature", HW_ID, HardwareKind.CPU, HW_NAME, SensorKind.TEMPERATURE, "CPU (EC)", v["cpu_temp"], "C"),
        ]
        # Fans: only report the ones the firmware lists (a missing fan is not "0 RPM").
        if ID_CPU_FAN in self._ids:
            out.append(RawSensor("/lenovo-gamezone/fan/cpu", HW_ID, HardwareKind.OTHER, HW_NAME, SensorKind.FAN, "CPU Fan", v["cpu_fan"], "RPM"))
        if ID_GPU_FAN in self._ids:
            out.append(RawSensor("/lenovo-gamezone/fan/gpu", HW_ID, HardwareKind.OTHER, HW_NAME, SensorKind.FAN, "GPU Fan", v["gpu_fan"], "RPM"))
        return out
