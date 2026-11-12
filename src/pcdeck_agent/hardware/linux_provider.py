"""Linux provider: procfs/sysfs + hwmon + RAPL + NVML, same RawSensor model as the Windows provider.

What needs what:
- CPU load (psutil), clock (``scaling_cur_freq``, else ``/proc/cpuinfo`` "cpu MHz"), model name,
  temperatures (hwmon), storage temperatures, fans, AMD/Intel GPU data: any user.
- CPU package power: Intel RAPL through powercap (``intel-rapl:0/energy_uj``, also on AMD Zen with
  Linux >= 5.8). The counter is root-only since Linux 5.10 (CVE-2020-8694): without access it is
  null and the provider says so in its notes (run elevated, or the udev rule in the README).
- NVIDIA: NVML (``nvidia-ml-py`` + the proprietary driver).
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import linux_hwmon
from .linux_gpu import DrmGpus, NvmlGpus
from .models import HardwareKind, RawSensor, SensorKind
from .provider import OsMemory, ProviderStatus, clean_cpu_name, psutil_memory
from .sysfs import read_int, read_text, subdirs

log = logging.getLogger(__name__)

CPU_HW = "/cpu/0"
RAPL_DIR = Path("sys/class/powercap/intel-rapl:0")
NOTE_RAPL_DENIED = "not running as root: CPU package power (RAPL) unavailable; see the README for the udev rule"
NOTE_RAPL_ROOT_DENIED = "CPU package power: the RAPL energy counter is not readable"
NOTE_RAPL_ABSENT = "CPU package power: this CPU/kernel exposes no RAPL energy counter"
NOTE_NVIDIA = "NVIDIA GPU without NVML (proprietary driver + nvidia-ml-py): only its name is shown"


def rapl_power_w(prev: tuple[int, float] | None, cur: tuple[int, float], max_range_uj: int | None) -> float | None:
    """Average power between two (energy µJ, monotonic s) samples, handling counter wrap-around."""
    if prev is None:
        return None
    de, dt = cur[0] - prev[0], cur[1] - prev[1]
    if dt <= 0:
        return None
    if de < 0:  # the counter wrapped at max_energy_range_uj
        if not max_range_uj:
            return None
        de += max_range_uj
    return de / dt / 1e6


class Rapl:
    """Package-0 energy counter of the powercap ``intel-rapl`` driver."""

    def __init__(self, directory: Path, clock: Callable[[], float] = time.monotonic) -> None:
        self.dir = directory
        self.clock = clock
        self.state = "unknown"  # ok | denied | absent
        self._prev: tuple[int, float] | None = None
        self._max = read_int(self.dir / "max_energy_range_uj")

    def _read_energy(self) -> int:
        return int((self.dir / "energy_uj").read_text().strip())

    def read(self) -> float | None:
        if not (self.dir / "energy_uj").exists() or not (read_text(self.dir / "name") or "package").startswith("package"):
            self.state = "absent"
            return None
        try:
            e = self._read_energy()
        except PermissionError:
            self.state = "denied"
            return None
        except (OSError, ValueError):
            self.state = "absent"
            return None
        self.state = "ok"
        now = self.clock()
        w = rapl_power_w(self._prev, (e, now), self._max)
        self._prev = (e, now)
        return w


def cpu_model(root: Path) -> str | None:
    text = read_text(root / "proc" / "cpuinfo") or ""
    for key in ("model name", "Model", "Hardware", "cpu model"):
        m = re.search(rf"^{key}\s*:\s*(.+)$", text, re.MULTILINE)
        if m:
            return clean_cpu_name(m.group(1))
    return None


def cpu_clock_mhz(root: Path) -> float | None:
    """Highest current core clock (cpufreq), or the highest "cpu MHz" in /proc/cpuinfo (VMs, WSL)."""
    khz = [read_int(d / "cpufreq" / "scaling_cur_freq") for d in subdirs(root / "sys" / "devices" / "system" / "cpu", "cpu[0-9]*")]
    valid = [k for k in khz if k]
    if valid:
        return max(valid) / 1000.0
    mhz = re.findall(r"^cpu MHz\s*:\s*([\d.]+)", read_text(root / "proc" / "cpuinfo") or "", re.MULTILINE)
    vals = [float(x) for x in mhz if float(x) > 0]
    return max(vals) if vals else None


def _psutil_cpu_percent() -> float | None:
    try:
        import psutil

        return float(psutil.cpu_percent(interval=None))
    except Exception:
        return None


class LinuxProvider:
    def __init__(
        self,
        root: Path | str = "/",
        *,
        nvml: Any = None,
        cpu_percent: Callable[[], float | None] = _psutil_cpu_percent,
        memory: Callable[[], OsMemory | None] = psutil_memory,
        clock: Callable[[], float] = time.monotonic,
        euid: int | None = None,
        rapl_dir: Path | None = None,  # tests: Windows file names cannot contain "intel-rapl:0"'s colon
    ) -> None:
        self.root = Path(root)
        self._cpu_percent = cpu_percent
        self._memory = memory
        self._euid = euid
        self._nvml = NvmlGpus(nvml)
        self._drm: DrmGpus | None = None
        self._rapl = Rapl(rapl_dir or self.root / RAPL_DIR, clock)
        self._chips: list[linux_hwmon.Chip] = []
        self._model = "CPU"
        self._opened = False

    def open(self) -> None:
        self._model = cpu_model(self.root) or "CPU"
        self._chips = linux_hwmon.scan(self.root)
        self._nvml.open()
        self._drm = DrmGpus(self.root, skip_nvidia=self._nvml.ok)
        self._drm.scan()
        self._cpu_percent()  # psutil: the first call only sets the baseline
        self._rapl.read()  # energy baseline + access check (status() notes are right from the start)
        self._opened = True
        log.info("Linux provider: cpu=%s, hwmon chips=%s, nvml=%s, drm gpus=%d", self._model,
                 ",".join(c.name for c in self._chips) or "none", self._nvml.ok, len(self._drm.cards))  # fmt: skip

    def read(self) -> list[RawSensor]:
        if not self._opened:
            raise RuntimeError("provider not opened")
        m = self._model
        cpu = HardwareKind.CPU
        out = [
            RawSensor(f"{CPU_HW}/load/0", CPU_HW, cpu, m, SensorKind.LOAD, "CPU Total", self._cpu_percent(), "%"),
            RawSensor(f"{CPU_HW}/clock/0", CPU_HW, cpu, m, SensorKind.CLOCK, "Core Clock (highest)", cpu_clock_mhz(self.root), "MHz"),
        ]
        power = self._rapl.read()
        if self._rapl.state != "absent":
            out.append(RawSensor(f"{CPU_HW}/power/0", CPU_HW, cpu, m, SensorKind.POWER, "CPU Package", power, "W"))
        out += linux_hwmon.cpu_temperatures(self._chips, CPU_HW, m)
        out += self._nvml.read()
        if self._drm is not None:
            out += self._drm.read()
        out += linux_hwmon.storage_temperatures(self._chips)
        out += linux_hwmon.other_temperatures(self._chips)
        out += linux_hwmon.fans(self._chips)
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
        if self._rapl.state == "denied":
            notes.append(NOTE_RAPL_ROOT_DENIED if root else NOTE_RAPL_DENIED)
        elif self._rapl.state == "absent":
            notes.append(NOTE_RAPL_ABSENT)
        if self._drm is not None and self._drm.nvidia_without_nvml:
            notes.append(NOTE_NVIDIA)
        name = "Linux hwmon/sysfs" + (" + NVML" if self._nvml.ok else "")
        return ProviderStatus(name=name, is_admin=root, notes=tuple(notes))

    def close(self) -> None:
        self._nvml.close()
        self._opened = False
