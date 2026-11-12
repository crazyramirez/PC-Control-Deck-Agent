"""Album art from a URL a local player publishes (Linux ``mpris:artUrl``, Spotify on macOS).

Only ``file://`` and ``http(s)://`` are accepted, at most 4 MiB, 3 s timeout. Remote covers are
downloaded in a background thread (e.g. Spotify's CDN, the same image the player shows), once per URL;
the result is converted with ``artwork.process`` into the deck's RGB565 frames, exactly as on Windows.
"""

from __future__ import annotations

import logging
import threading
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MAX_BYTES = 4 * 1024 * 1024
TIMEOUT_S = 3.0


def load_art_bytes(url: str, max_bytes: int = MAX_BYTES, timeout: float = TIMEOUT_S) -> bytes | None:
    try:
        u = urllib.parse.urlsplit(url)
        if u.scheme == "file":
            p = Path(urllib.request.url2pathname(urllib.parse.unquote(u.path)))
            if not p.is_file() or p.stat().st_size > max_bytes:
                return None
            return p.read_bytes()
        if u.scheme in ("http", "https"):
            req = urllib.request.Request(url, headers={"User-Agent": "pcdeck-agent"})
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (scheme checked above)
                data = r.read(max_bytes + 1)
            return data if len(data) <= max_bytes else None
    except Exception as e:
        log.debug("artwork %s unavailable: %s", url[:80], type(e).__name__)
    return None


def _process(data: bytes) -> Any:
    from .artwork import process

    return process(data)


class ArtFetcher:
    """``get(url)`` returns the processed cover for ``url`` once loaded (None until then / on failure)."""

    def __init__(
        self,
        loader: Callable[[str], bytes | None] = load_art_bytes,
        process: Callable[[bytes], Any] = _process,
        background: bool = True,
    ) -> None:
        self._loader = loader
        self._process = process
        self._background = background
        self._lock = threading.Lock()
        self._url: str | None = None
        self._art: Any = None

    def _load(self, url: str) -> None:
        art = None
        data = self._loader(url)
        if data:
            try:
                art = self._process(data)
            except Exception:
                log.debug("artwork decode failed", exc_info=True)
        with self._lock:
            if self._url == url:
                self._art = art

    def get(self, url: str | None) -> Any:
        if not url:
            with self._lock:
                self._url, self._art = None, None
            return None
        with self._lock:
            if url == self._url:
                return self._art
            self._url, self._art = url, None
        if self._background and not url.startswith("file:"):
            threading.Thread(target=self._load, args=(url,), name="pcdeck-art", daemon=True).start()
        else:
            self._load(url)
        with self._lock:
            return self._art

    def reset(self) -> None:
        """Forget the cached URL (e.g. a player rewrote the same file with a new cover)."""
        with self._lock:
            self._url, self._art = None, None
