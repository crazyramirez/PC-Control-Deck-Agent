"""Tiny helpers for reading Linux sysfs / procfs attributes (pure Python, never raise).

Every reader takes paths below an injectable root, so tests run against fake trees in ``tmp_path``.
"""

from __future__ import annotations

import re
from pathlib import Path

_TRAILING_NUM = re.compile(r"(\d+)$")


def read_text(path: Path) -> str | None:
    """Stripped file content; None when missing, unreadable or busy (EIO/ENODATA from a driver)."""
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except (OSError, ValueError):
        return None


def read_int(path: Path) -> int | None:
    s = read_text(path)
    if s is None:
        return None
    try:
        return int(s, 0) if s.lower().startswith("0x") else int(s)
    except ValueError:
        return None


def by_number(paths: list[Path]) -> list[Path]:
    """hwmon2 < hwmon10 (numeric, not lexical)."""

    def key(p: Path) -> tuple[int, str]:
        m = _TRAILING_NUM.search(p.name)
        return (int(m.group(1)) if m else -1, p.name)

    return sorted(paths, key=key)


def subdirs(base: Path, pattern: str) -> list[Path]:
    try:
        return by_number([p for p in base.glob(pattern) if p.is_dir()])
    except OSError:
        return []
