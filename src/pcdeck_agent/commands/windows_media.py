"""Windows media / volume / session backend.

All COM (Core Audio via pycaw) and WinRT (Global System Media Transport Controls) calls run on ONE
dedicated thread with its own asyncio loop, because both technologies are thread-affine.

- Media keys go to the Windows "SMTC" session: Spotify, browsers playing audio (Chrome/Edge Media
  Session — e.g. web players), VLC, Groove... Selection is sticky: the player the deck shows keeps
  the focus; another one takes over only after playing for SWITCH_AFTER_S (or at once if the
  current player is gone). Track changes get a GRACE_S hold so a transient pause / empty title /
  sibling browser tab never flashes on the deck.
- With no session at all, virtual media keys are sent as a fallback (Windows routes them).
- Installed web apps (e.g. a Chrome "_crx_" app) are named after their Start-menu entry.
- Volume is the default playback device master volume (0–100 %).
- Lock = LockWorkStation (the interactive session the agent runs in).
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Callable, Coroutine

from . import services, start_apps

log = logging.getLogger(__name__)

# WinRT GlobalSystemMediaTransportControlsSessionPlaybackStatus
_PLAYING = 4

SWITCH_AFTER_S = 3.0  # another player must play this long before it replaces the shown one
GRACE_S = 4.0  # hold the shown track while the player changes tracks
ART_RECHECK_S = 1.5  # after a track change the cover is re-read this often...
ART_SETTLE_S = 12.0  # ...for this long (and up to ART_MISSING_S while there is none at all)
ART_MISSING_S = 60.0

VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_PLAY_PAUSE = 0xB3
KEYEVENTF_KEYUP = 0x0002


@dataclass
class MediaState:
    volume_pct: int | None = None
    muted: bool | None = None
    title: str | None = None
    artist: str | None = None
    app: str | None = None
    playing: bool | None = None
    pos_s: int | None = None  # playback position (seconds), extrapolated while playing
    dur_s: int | None = None
    can_seek: bool | None = None  # the player accepts position changes
    art: Any = None  # artwork.Artwork | None


_KNOWN = {"spotify": "Spotify", "chrome": "Chrome", "msedge": "Edge", "firefox": "Firefox", "vlc": "VLC",
          "whatsapp": "WhatsApp", "zunemusic": "Media Player", "opera": "Opera", "brave": "Brave",
          "music.ui": "Media Player"}


def friendly_app(aumid: str | None) -> str | None:
    if not aumid:
        return None
    low = aumid.lower()
    # Installed web apps ("Chrome._crx_<id>", Edge "_crx_" too) and unknown apps: Start-menu name,
    # i.e. the name the user gave the app. Well-known players keep their short names below.
    installed = start_apps.name_for(aumid)
    if installed and ("_crx_" in low or not any(k in low for k in _KNOWN)):
        return installed[:24]
    for key, name in _KNOWN.items():
        if key in low:
            return name
    base = aumid.split("!")[0].split("\\")[-1]
    return base.removesuffix(".exe")[:24] or None


@dataclass
class _Sess:
    s: Any
    aumid: str
    playing: bool
    title: str
    props: Any


class WindowsMedia:
    def __init__(self) -> None:
        start_apps.preload_async()
        self._cur: tuple[str, str] | None = None  # (AUMID, title) the deck is showing
        self._chosen: Any = None  # session picked by the last state read (commands target it)
        self._rival_since: dict[tuple[str, str], float] = {}
        self._shown: MediaState | None = None
        self._shown_playing_t = 0.0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._manager: Any = None
        self._endpoint: Any = None
        self._art_key: tuple | None = None
        self._art: Any = None
        self._art_at = 0.0  # monotonic time of the last track change
        self._art_next = 0.0  # next artwork re-check
        self._tiles: dict[str, Any] = {}  # generated covers per online service
        self.audio_ok = False
        self.media_ok = False

    # ---- thread ---------------------------------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="pcdeck-media", daemon=True)
        self._thread.start()
        self._ready.wait(10)

    def _run(self) -> None:
        # MTA, not STA: WinRT async completions (thumbnail streams) would need a message pump in an
        # STA thread and hang forever. Core Audio is free-threaded and works in the MTA.
        # comtypes initialises COM in the importing thread using sys.coinit_flags, so set it before
        # the first import (this thread is the only one importing comtypes; Qt's GUI thread stays STA).
        import sys

        already = "comtypes" in sys.modules
        sys.coinit_flags = 0  # COINIT_MULTITHREADED
        import comtypes

        if already:
            try:
                comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
            except OSError:
                log.warning("COM already initialised in another mode on the media thread")
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._init())
        finally:
            self._ready.set()
        self._loop.run_forever()

    async def _init(self) -> None:
        try:
            from pycaw.pycaw import AudioUtilities

            self._endpoint = AudioUtilities.GetSpeakers().EndpointVolume
            self._endpoint.GetMasterVolumeLevelScalar()
            self.audio_ok = True
        except Exception as e:
            log.warning("Core Audio unavailable: %s", e)
        try:
            from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as M

            self._manager = await M.request_async()
            self.media_ok = True
        except Exception as e:
            log.warning("media transport controls unavailable (media keys fallback only): %s", e)
        log.info("media backend: audio=%s smtc=%s", self.audio_ok, self.media_ok)

    def call(self, coro_fn: Callable[[], Coroutine[Any, Any, Any]], timeout: float = 3.0) -> Any:
        """Run a coroutine on the media thread and wait for its result (from any other thread)."""
        if not self._loop:
            raise RuntimeError("media backend not started")
        fut: Future = asyncio.run_coroutine_threadsafe(coro_fn(), self._loop)
        return fut.result(timeout)

    def stop(self) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    # ---- helpers (media thread) -------------------------------------------------------------------
    async def _sessions(self) -> list[_Sess]:
        out: list[_Sess] = []
        if not self._manager:
            return out
        for x in list(self._manager.get_sessions()):
            try:
                props = await x.try_get_media_properties_async()
                out.append(
                    _Sess(x, x.source_app_user_model_id or "", x.get_playback_info().playback_status == _PLAYING,
                          (props.title or "") if props else "", props)
                )
            except Exception:
                continue
        return out

    async def _pick(self) -> _Sess | None:
        """Sticky session choice (see module doc)."""
        return pick_session(self, await self._sessions(), time.monotonic())

    @staticmethod
    def _key(vk: int) -> None:
        user32 = ctypes.windll.user32
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)

    # ---- actions (coroutines executed on the media thread) -------------------------------------------
    async def a_set_volume(self, pct: int) -> int:
        if not self.audio_ok:
            raise RuntimeError("no audio endpoint")
        self._endpoint.SetMasterVolumeLevelScalar(max(0, min(100, pct)) / 100.0, None)
        if pct > 0 and self._endpoint.GetMute():
            self._endpoint.SetMute(0, None)  # raising the volume from the deck implies "unmute"
        return round(self._endpoint.GetMasterVolumeLevelScalar() * 100)

    async def a_media(self, op: str) -> bool:
        """Send play/pause/next/previous to the detected player (media key when there is none)."""
        s = self._chosen
        if s is None:
            picked = await self._pick()
            s = picked.s if picked else None
        if s is not None:
            fn = {"play_pause": s.try_toggle_play_pause_async, "next": s.try_skip_next_async, "previous": s.try_skip_previous_async}[op]
            ok = await fn()
            if ok:
                return True
        # No session (or it refused): global media key, Windows routes it to the active player.
        self._key({"play_pause": VK_MEDIA_PLAY_PAUSE, "next": VK_MEDIA_NEXT_TRACK, "previous": VK_MEDIA_PREV_TRACK}[op])
        return True

    async def a_seek(self, pos_s: int) -> bool:
        """Jump to pos_s seconds into the current track (players that support it, e.g. Spotify, browsers)."""
        s = self._chosen
        if s is None:
            return False
        tl = s.get_timeline_properties()
        start = tl.start_time.total_seconds()
        end = (tl.end_time - tl.start_time).total_seconds()
        pos = max(0.0, min(float(pos_s), end)) if end > 0 else float(pos_s)
        # SMTC takes the absolute position in 100 ns ticks.
        return bool(await s.try_change_playback_position_async(int((start + pos) * 10_000_000)))

    async def a_state(self) -> MediaState:
        st = MediaState()
        if self.audio_ok:
            try:
                st.volume_pct = round(self._endpoint.GetMasterVolumeLevelScalar() * 100)
                st.muted = bool(self._endpoint.GetMute())
            except Exception:
                pass
        picked = await self._pick()
        self._chosen = picked.s if picked else None
        if picked is not None:
            s, props = picked.s, picked.props
            try:
                st.title = (props.title or None) if props else None
                st.artist = (props.artist or None) if props else None
                info = s.get_playback_info()
                st.playing = info.playback_status == _PLAYING
                try:
                    st.can_seek = bool(info.controls.is_playback_position_enabled)
                except Exception:
                    st.can_seek = None
                st.app = friendly_app(s.source_app_user_model_id)
                st.pos_s, st.dur_s = self._timeline(s, bool(st.playing))
                # A known site playing in a browser tab without metadata (e.g. Instagram): its own
                # cover instead of the browser logo, and "Instagram · Chrome" as the source.
                svc = services.detect(st.app, st.title, st.artist)
                if svc is not None:
                    st.title = services.clean_title(svc, st.title or "")
                    st.app = f"{svc.name} · {st.app}"
                key = (st.app, st.title, st.artist)
                now = time.monotonic()
                if key != self._art_key:  # fetch/convert artwork only when the track changes
                    self._art_key = key
                    self._art_at = now
                    self._art_next = now + ART_RECHECK_S
                    self._art = self._service_tile(svc) if svc is not None else await self._artwork(props)
                elif svc is None and now >= self._art_next and (
                    now - self._art_at < ART_SETTLE_S or (self._art is None and now - self._art_at < ART_MISSING_S)
                ):
                    # Players publish the new title before its thumbnail: re-read the cover for a few
                    # seconds after a change (it may be missing, or still the previous track's).
                    self._art_next = now + ART_RECHECK_S
                    fresh = await self._artwork(props)
                    if fresh is not None:
                        self._art = fresh
                st.art = self._art
            except Exception:
                log.debug("media state failed", exc_info=True)
        else:
            self._art_key, self._art = None, None
        return hold_track(self, st, time.monotonic())

    def _service_tile(self, svc: Any) -> Any:
        if svc.name not in self._tiles:
            try:
                from .artwork import service_tile

                self._tiles[svc.name] = service_tile(svc)
            except Exception:
                log.debug("service cover failed", exc_info=True)
                self._tiles[svc.name] = None
        return self._tiles[svc.name]

    @staticmethod
    def _timeline(s: Any, playing: bool) -> tuple[int | None, int | None]:
        import datetime as dt

        try:
            tl = s.get_timeline_properties()
            end = (tl.end_time - tl.start_time).total_seconds()
            pos = (tl.position - tl.start_time).total_seconds()
            if end <= 0:
                return None, None
            if playing and tl.last_updated_time:
                # position is a snapshot taken at last_updated_time: extrapolate to now
                pos += (dt.datetime.now(dt.timezone.utc) - tl.last_updated_time).total_seconds()
            return int(max(0.0, min(pos, end))), int(end)
        except Exception:
            return None, None

    @staticmethod
    async def _artwork(props: Any) -> Any:
        if props is None or props.thumbnail is None:
            return None
        try:
            from winrt.windows.storage.streams import DataReader

            from .artwork import process

            stream = await props.thumbnail.open_read_async()
            n = int(stream.size)
            if n <= 0 or n > 4 * 1024 * 1024:
                return None
            reader = DataReader(stream.get_input_stream_at(0))
            await reader.load_async(n)
            buf = bytearray(n)
            reader.read_bytes(buf)
            return process(bytes(buf))
        except Exception:
            log.debug("artwork unavailable", exc_info=True)
            return None


def lock_workstation() -> bool:
    return bool(ctypes.windll.user32.LockWorkStation())


# ---- pure selection logic (unit-tested without WinRT) ---------------------------------------------
def pick_session(m: Any, infos: list, now: float) -> Any:
    """Sticky choice among sessions (objects with aumid/playing/title). Updates m._cur / m._rival_since."""
    if not infos:
        m._cur = None
        return None
    cur = m._cur
    mine = [i for i in infos if cur and i.aumid == cur[0]]
    exact = [i for i in mine if i.title == cur[1]] if cur else []
    others = [i for i in infos if i.playing and not (cur and i.aumid == cur[0])]
    alive = {(i.aumid, i.title) for i in others}
    m._rival_since = {k: v for k, v in m._rival_since.items() if k in alive}
    for i in others:
        m._rival_since.setdefault((i.aumid, i.title), now)
    matured = [i for i in others if now - m._rival_since[(i.aumid, i.title)] >= SWITCH_AFTER_S]

    choice = next((i for i in exact if i.playing), None) or next((i for i in mine if i.playing), None)
    if choice is None and matured:
        choice = matured[0]
    if choice is None and mine:
        choice = (exact or mine)[0]  # the shown player is paused / between tracks: keep it
    if choice is None and others:
        choice = others[0]  # the shown player is gone (or nothing shown yet): follow what plays
    if choice is None:
        current = m._manager.get_current_session() if getattr(m, "_manager", None) else None
        cid = current.source_app_user_model_id if current else None
        choice = next((i for i in infos if i.aumid == cid), infos[0])
    if choice.title or not cur or cur[0] != choice.aumid:
        m._cur = (choice.aumid, choice.title)
    return choice


def hold_track(m: Any, st: MediaState, now: float) -> MediaState:
    """Keep showing the last track for GRACE_S while the player switches tracks."""
    shown = m._shown
    changed = shown is not None and (st.app, st.title) != (shown.app, shown.title)
    if shown is not None and changed and not st.playing and now - m._shown_playing_t < GRACE_S:
        return MediaState(**{**shown.__dict__, "volume_pct": st.volume_pct, "muted": st.muted})
    m._shown = st
    if st.playing:
        m._shown_playing_t = now
    return st
