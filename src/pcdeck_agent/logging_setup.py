"""Console + rotating file logging. Never log secrets (Wi-Fi keys, tokens, pairing codes)."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

FMT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging(level: str = "INFO", log_dir: Path | None = None) -> Path | None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(FMT))
    root.addHandler(console)
    if log_dir is None:
        return None
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "agent.log"
    fh = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(logging.Formatter(FMT))
    root.addHandler(fh)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    return path
