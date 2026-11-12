"""macOS provider: psutil + IOKit (ctypes) + ioreg + powermetrics, same RawSensor model as Windows.

| Metric | Source | Needs |
|---|---|---|
| CPU load, RAM | psutil | - |
| CPU model, GPU names | ``sysctl machdep.cpu.brand_string``, ``system_profiler SPDisplaysDataType`` (cached) | - |
| CPU / GPU / SSD temperatures | Apple Silicon: IOHIDEventSystemClient; Intel: AppleSMC keys | - |
| Fans | AppleSMC ``FNum`` / ``F<n>Ac`` | - |
| GPU load (+ VRAM on AMD) | ``ioreg -c IOAccelerator`` "PerformanceStatistics" | - |
| CPU / GPU power, CPU clock | ``powermetrics`` (streamed in a thread) | root |

macOS exposes only the *nominal* CPU frequency to normal users (psutil reports that constant), so
the clock is null unless powermetrics runs. Apple Silicon GPUs share system memory: no VRAM figures.
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any

from .macos_parsers import (
    INTEL_KEYS,
    AccelStats,
    DisplayGpu,
    hid_temperatures,
    parse_ioaccelerator,
    parse_powermetrics,
    parse_system_profiler_displays,
    smc_intel_temperatures,
)
from .models import HardwareKind, RawSensor, SensorKind
from .provider import OsMemory, ProviderStatus, clean_cpu_name, psutil_memory

log = logging.getLogger(__name__)

CPU_HW = "/cpu/0"
NOTE_NOT_ROOT = "not running as root: CPU/GPU power and CPU clock unavailable (powermetrics needs sudo)"
NOTE_NO_CPU_TEMP = "CPU temperature unavailable: no readable IOKit/SMC temperature sensor"
Runner = Callable[..., "str | None"]


def run_cmd(cmd: list[str], timeout: float = 5.0) -> str | None:
    """stdout of a command, or None (missing tool, error, timeout)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def _psutil_cpu_percent() -> float | None:
    try:
        import psutil

        return float(psutil.cpu_percent(interval=None))
    except Exception:
        return None


class Powermetrics:
    """``powermetrics --samplers cpu_power,gpu_power -i 1000`` streamed in a thread (root only)."""

    CMD = ["powermetrics", "--samplers", "cpu_power,gpu_power", "-i", "1000"]
    STALE_S = 5.0

    def __init__(self, popen: Callable[..., Any] = subprocess.Popen, clock: Callable[[], float] = time.monotonic) -> None:
        self._popen = popen
        self._clock = clock
        self._proc: Any = None
        self._buf: list[str] = []
        self._latest: dict[str, float] = {}
        self._at = 0.0

    def start(self) -> bool:
        try:
            self._proc = self._popen(self.CMD, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        except OSError as e:
            log.info("powermetrics unavailable: %s", e)
            return False
        threading.Thread(target=self._pump, name="pcdeck-powermetrics", daemon=True).start()
        return True

    def feed(self, line: str) -> None:
        if line.startswith("*** Sampled system activity"):
            self._flush()
        else:
            self._buf.append(line)

    def _flush(self) -> None:
        if self._buf:
            sample = parse_powermetrics("".join(self._buf))
            if sample:
                self._latest, self._at = sample, self._clock()
        self._buf = []

    def _pump(self) -> None:
        try:
            for line in self._proc.stdout:
                self.feed(line)
        except Exception:
            log.debug("powermetrics stream ended", exc_info=True)

    def latest(self) -> dict[str, float]:
        if not self._latest or self._clock() - self._at > self.STALE_S:
            return {}
        return dict(self._latest)

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
        self._proc = None


def _vendor(ioclass: str) -> str:
    c = ioclass.lower()
    for key, vendor in (("agx", "apple"), ("amd", "amd"), ("radeon", "amd"), ("intel", "intel"), ("nvda", "nvidia"), ("nvidia", "nvidia")):
        if key in c:
            return vendor
    return ""


class MacProvider:
    def __init__(
        self,
        runner: Runner = run_cmd,
        *,
        hid: Any = None,
        smc: Any = None,
        powermetrics: Any = None,
        cpu_percent: Callable[[], float | None] = _psutil_cpu_percent,
        memory: Callable[[], OsMemory | None] = psutil_memory,
        euid: int | None = None,
        arch: str | None = None,
    ) -> None:
        self._run = runner
        self._hid = hid
        self._smc = smc
        self._pm = powermetrics
        self._cpu_percent = cpu_percent
        self._memory = memory
        self._euid = euid
        self._arch = arch
        self._arm = False
        self._model = "Mac CPU"
        self._displays: list[DisplayGpu] = []
        self._fans = 0
        self._hid_ok = self._smc_ok = self._pm_ok = False
        self._cpu_temp_seen = False
        self._reads = 0

    # ---- lifecycle --------------------------------------------------------------------------------
    def open(self) -> None:
        from .macos_iokit import HidThermals, Smc

        self._arm = (self._arch or platform.machine()) == "arm64"
        self._model = clean_cpu_name(self._run(["sysctl", "-n", "machdep.cpu.brand_string"])) or "Mac CPU"
        self._displays = parse_system_profiler_displays(self._run(["system_profiler", "SPDisplaysDataType", "-json"], timeout=30) or "")
        if self._arm:
            self._hid = self._hid or HidThermals()
            self._hid_ok = bool(self._hid.open())
        self._smc = self._smc or Smc()
        self._smc_ok = bool(self._smc.open())
        if self._smc_ok:
            self._fans = int(self._smc.read("FNum") or 0)
        if self._is_root():
            self._pm = self._pm or Powermetrics()
            self._pm_ok = bool(self._pm.start())
        self._cpu_percent()  # psutil baseline
        log.info("macOS provider: cpu=%s arm64=%s hid=%s smc=%s fans=%d powermetrics=%s gpus=%s", self._model, self._arm,
                 self._hid_ok, self._smc_ok, self._fans, self._pm_ok, [d.name for d in self._displays])  # fmt: skip

    def close(self) -> None:
        for part in (self._hid, self._smc):
            if part is not None:
                try:
                    part.close()
                except Exception:
                    pass
        if self._pm is not None:
            self._pm.stop()

    # ---- reading ------------------------------------------------------------------------------------
    def _gpus(self, accel: list[AccelStats]) -> list[tuple[str, str, HardwareKind, str, AccelStats | None]]:
        """(hw id, name, kind, vendor, stats) per GPU: IOAccelerator first, system_profiler names otherwise."""
        out = []
        sources = accel or [None] * len(self._displays)
        for i, a in enumerate(sources):
            vendor = _vendor(a.ioclass) if a else (self._displays[i].vendor if i < len(self._displays) else "")
            disp = next((d for d in self._displays if d.vendor == vendor), self._displays[i] if i < len(self._displays) else None)
            name = (a.model if a and a.model else None) or (disp.name if disp else None) or (a.ioclass if a else "GPU")
            kind = HardwareKind.GPU_DISCRETE if vendor in ("amd", "nvidia") else HardwareKind.GPU_INTEGRATED
            out.append((f"/gpu-{vendor or 'mac'}/{i}", name, kind, vendor, a))
        return out

    def read(self) -> list[RawSensor]:
        m, cpu = self._model, HardwareKind.CPU
        pm = self._pm.latest() if self._pm_ok and self._pm is not None else {}
        out = [
            RawSensor(f"{CPU_HW}/load/0", CPU_HW, cpu, m, SensorKind.LOAD, "CPU Total", self._cpu_percent(), "%"),
            RawSensor(f"{CPU_HW}/clock/0", CPU_HW, cpu, m, SensorKind.CLOCK, "Core Clock (highest)", pm.get("cpu_mhz"), "MHz"),
            RawSensor(f"{CPU_HW}/power/0", CPU_HW, cpu, m, SensorKind.POWER, "CPU Package", pm.get("cpu_w"), "W"),
        ]
        gpus = self._gpus(parse_ioaccelerator(self._run(["ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"]) or ""))
        for hw, name, kind, vendor, a in gpus:
            out.append(RawSensor(f"{hw}/load/0", hw, kind, name, SensorKind.LOAD, "GPU Core", a.utilization if a else None, "%"))
            if a and a.vram_used_mib is not None:
                total = a.vram_used_mib + a.vram_free_mib if a.vram_free_mib is not None else None
                if total is None:
                    total = next((d.vram_mib for d in self._displays if d.vendor == vendor and d.vram_mib), None)
                out.append(RawSensor(f"{hw}/smalldata/1", hw, kind, name, SensorKind.DATA, "GPU Memory Used", a.vram_used_mib, "MiB"))
                out.append(RawSensor(f"{hw}/smalldata/2", hw, kind, name, SensorKind.DATA, "GPU Memory Total", total, "MiB"))
            if vendor == "apple":
                out.append(RawSensor(f"{hw}/power/0", hw, kind, name, SensorKind.POWER, "GPU Power", pm.get("gpu_w"), "W"))
            elif a and a.temp_c is not None:
                out.append(RawSensor(f"{hw}/temperature/0", hw, kind, name, SensorKind.TEMPERATURE, "GPU Core", a.temp_c, "C"))
        apple = next(((hw, name, kind) for hw, name, kind, v, _ in gpus if v == "apple"), None)
        main = next(((hw, name, kind) for hw, name, kind, _, _ in gpus if kind == HardwareKind.GPU_DISCRETE), None) or (
            (gpus[0][0], gpus[0][1], gpus[0][2]) if gpus else None
        )
        if self._hid_ok:
            out += hid_temperatures(self._hid.read(), (CPU_HW, m), apple)
        elif self._smc_ok and not self._arm:
            out += smc_intel_temperatures({k: self._smc.read(k) for k in INTEL_KEYS}, (CPU_HW, m), main)
        if self._smc_ok:
            for i in range(self._fans):
                out.append(RawSensor(f"/smc/fan/{i}", "/smc", HardwareKind.OTHER, "SMC", SensorKind.FAN, f"Fan #{i + 1}",
                                     self._smc.read(f"F{i}Ac"), "RPM"))  # fmt: skip
        self._reads += 1
        self._cpu_temp_seen = any(r.hardware_kind == cpu and r.kind == SensorKind.TEMPERATURE and r.value is not None for r in out)
        return out

    def os_memory(self) -> OsMemory | None:
        return self._memory()

    def _is_root(self) -> bool:
        if self._euid is not None:
            return self._euid == 0
        return hasattr(os, "geteuid") and os.geteuid() == 0

    def status(self) -> ProviderStatus:
        root = self._is_root()
        notes: list[str] = []
        if not root or not self._pm_ok:
            notes.append(NOTE_NOT_ROOT if not root else "CPU/GPU power and CPU clock unavailable: powermetrics did not start")
        if not (self._hid_ok or self._smc_ok) or (self._reads and not self._cpu_temp_seen):
            notes.append(NOTE_NO_CPU_TEMP)
        parts = ["IOHID" if self._hid_ok else "", "SMC" if self._smc_ok else "", "powermetrics" if self._pm_ok else ""]
        return ProviderStatus(name="macOS " + " + ".join(p for p in ["ioreg", *parts] if p), is_admin=root, notes=tuple(notes))
