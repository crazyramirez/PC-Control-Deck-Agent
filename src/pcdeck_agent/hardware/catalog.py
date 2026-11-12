"""Sensor catalog (stable id, kind, unit, hardware) and a redacted diagnostic report."""

from __future__ import annotations

import platform
import sys
from collections.abc import Iterable
from dataclasses import asdict, dataclass

from .mapper import sanitize
from .models import RawSensor, TelemetrySnapshot
from .provider import ProviderStatus


@dataclass(frozen=True)
class CatalogEntry:
    id: str
    name: str
    kind: str
    unit: str
    hardware: str
    hardware_kind: str
    quality: str


def build_catalog(raw: Iterable[RawSensor]) -> list[CatalogEntry]:
    entries = []
    for r in raw:
        _, q = sanitize(r)
        entries.append(
            CatalogEntry(
                id=r.id,
                name=r.name,
                kind=r.kind.value,
                unit=r.unit,
                hardware=r.hardware_name,
                hardware_kind=r.hardware_kind.value,
                quality=q.value,
            )
        )
    return sorted(entries, key=lambda e: e.id)


def diagnostic_report(status: ProviderStatus, raw: list[RawSensor], snapshot: TelemetrySnapshot) -> dict:
    """JSON-serializable report. Contains no secrets, usernames or network data by construction:
    only hardware names, sensor ids/values and software versions."""
    catalog = build_catalog(raw)
    by_quality: dict[str, int] = {}
    for e in catalog:
        by_quality[e.quality] = by_quality.get(e.quality, 0) + 1
    snap = asdict(snapshot)
    snap["host"] = "<redacted>"
    return {
        "provider": status.name,
        "is_admin": status.is_admin,
        "notes": list(status.notes),
        "python": sys.version.split()[0],
        "os": f"{platform.system()} {platform.release()} ({platform.version()})",
        "sensor_count": len(catalog),
        "sensors_by_quality": by_quality,
        "selected": {
            "cpu_temp_sensor_id": snapshot.cpu.temp_sensor_id,
            "gpu_hardware_id": snapshot.gpu.hardware_id,
        },
        "snapshot": snap,
        "catalog": [asdict(e) for e in catalog],
    }
