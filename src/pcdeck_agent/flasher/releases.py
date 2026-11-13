"""Firmware releases: the one bundled with the app, the latest on GitHub, and their images on disk.

The app ships each model's latest firmware at build time (``bundled.py``), so updates work offline.
When GitHub has a newer release, it is used instead (downloaded into a per-model cache). Only the
fixed repositories in ``boards.BOARDS`` are queried (GitHub REST API, no token: 60 requests per
hour). Every image is checked against its size and, when known, its SHA-256 before it is used.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import app_dir
from .boards import OWNER, Board, is_newer, parse_version

API = "https://api.github.com/repos/{owner}/{repo}/releases/latest"
USER_AGENT = "PCControlDeck-Agent"
TIMEOUT_S = 15
CHUNK = 64 * 1024


class ReleaseError(Exception):
    """The release could not be fetched or an image failed its checks (message for the log/UI)."""


@dataclass(frozen=True)
class Asset:
    name: str
    url: str  # "" for an image bundled with the app
    size: int
    sha256: str | None = None  # hex; from the API's "digest" field, or the bundle manifest
    path: Path | None = None  # bundled image on disk


@dataclass(frozen=True)
class Release:
    tag: str
    version: str  # "0.13.2"
    assets: dict[str, Asset] = field(default_factory=dict)
    source: str = "online"  # "online" (GitHub) / "bundled" (shipped with the app)


def cache_dir(board: Board) -> Path:
    """Downloaded images, one folder per model (the S3 boards share image names)."""
    return app_dir() / "firmware" / board.key


def parse_release(data: dict[str, Any]) -> Release:
    tag = str(data.get("tag_name") or "")
    if parse_version(tag) is None:
        raise ReleaseError(f"unexpected release tag {tag!r}")
    assets: dict[str, Asset] = {}
    for a in data.get("assets") or []:
        name, url = a.get("name"), a.get("browser_download_url")
        if not name or not url or not str(url).startswith("https://"):
            continue
        digest = str(a.get("digest") or "")
        sha = digest.split(":", 1)[1].lower() if digest.lower().startswith("sha256:") else None
        assets[name] = Asset(name, url, int(a.get("size") or 0), sha)
    return Release(tag, tag.lstrip("v"), assets)


def _get(url: str, accept: str, timeout: float, token: str | None = None) -> Any:
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 (https URLs only)


def latest(board: Board, timeout: float = TIMEOUT_S, token: str | None = None) -> Release:
    """``token``: build-time only (CI's GITHUB_TOKEN lifts the per-IP API limit); never sent with
    downloads, whose redirects leave github.com."""
    return latest_of(board.repo, timeout, token)


def latest_of(repo: str, timeout: float = TIMEOUT_S, token: str | None = None) -> Release:
    """The latest release of one of the project's repositories (``OWNER/<repo>``)."""
    url = API.format(owner=OWNER, repo=repo)
    try:
        with _get(url, "application/vnd.github+json", timeout, token) as r:
            data = json.loads(r.read().decode("utf-8"))
    except (OSError, ValueError) as e:
        raise ReleaseError(str(e)) from e
    return parse_release(data)


def pick(bundled: Release | None, online: Release | None) -> Release | None:
    """The release to install: GitHub's when it is newer than the bundled one, else the bundled
    one (no download needed)."""
    if online is not None and (bundled is None or is_newer(online.version, bundled.version)):
        return online
    return bundled or online


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def _valid(path: Path, asset: Asset) -> bool:
    return path.is_file() and path.stat().st_size == asset.size and (asset.sha256 is None or sha256_of(path) == asset.sha256)


def image_path(
    asset: Asset, dest: Path, progress: Callable[[int, int], None] | None = None, opener: Callable[[str], Any] | None = None
) -> Path:
    """The verified image on disk: the bundled file, or the download (cached in ``dest``)."""
    if asset.path is not None:
        if not _valid(asset.path, asset):
            raise ReleaseError(f"{asset.name}: bundled image is damaged")
        return asset.path
    return download(asset, dest, progress, opener=opener)


def download(
    asset: Asset,
    dest: Path,
    progress: Callable[[int, int], None] | None = None,
    timeout: float = TIMEOUT_S,
    opener: Callable[[str], Any] | None = None,
) -> Path:
    """The image in ``dest``, downloaded unless a verified copy is already there."""
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / asset.name
    if _valid(path, asset):
        if progress:
            progress(asset.size, asset.size)
        return path
    tmp = path.with_suffix(path.suffix + ".part")
    h = hashlib.sha256()
    done = 0
    try:
        with (opener(asset.url) if opener else _get(asset.url, "application/octet-stream", timeout)) as r, tmp.open("wb") as f:
            while True:
                block = r.read(CHUNK)
                if not block:
                    break
                f.write(block)
                h.update(block)
                done += len(block)
                if progress:
                    progress(done, asset.size)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise ReleaseError(str(e)) from e
    if done != asset.size:
        tmp.unlink(missing_ok=True)
        raise ReleaseError(f"{asset.name}: {done} bytes, expected {asset.size}")
    if asset.sha256 is not None and h.hexdigest() != asset.sha256:
        tmp.unlink(missing_ok=True)
        raise ReleaseError(f"{asset.name}: SHA-256 mismatch")
    os.replace(tmp, path)
    _prune(dest, keep=path)
    return path


def _prune(dest: Path, keep: Path) -> None:
    """Keep the cache small: older images of the same variant go (``pcdeck-<env>-<ver>...``)."""
    prefix = "-".join(keep.name.split("-")[:2]) + "-"  # "pcdeck-deck_r3-"
    merged = keep.name.endswith("-merged.bin")
    for p in dest.glob(prefix + "*.bin"):
        if p != keep and p.name.endswith("-merged.bin") == merged:
            p.unlink(missing_ok=True)
