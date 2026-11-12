"""Linux media / volume / session backend.

- Now playing + transport: MPRIS over the D-Bus session bus (``jeepney``, pure Python). Every
  ``org.mpris.MediaPlayer2.*`` player is read (Spotify, Firefox, Chromium/Chrome, VLC, mpv,
  Rhythmbox...); the choice uses the same sticky rules as Windows (``media_common.pick_session``),
  with players ordered by when they last played. Seek = ``SetPosition`` (or ``Seek`` without a track id).
- Album art: ``mpris:artUrl`` (``file://`` or ``http(s)://``), converted like on Windows; browser tabs
  without metadata get the same generated service covers.
- Volume: PipeWire ``wpctl`` (default sink), else PulseAudio ``pactl``.
- Lock / power: ``posix_session`` (loginctl, systemctl after a 10 s notice).
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import services
from .art_fetch import ArtFetcher
from .media_common import MediaState, hold_track, pick_session
from .posix_session import lock_linux, schedule_power

log = logging.getLogger(__name__)

MPRIS_PREFIX = "org.mpris.MediaPlayer2."
MPRIS_PATH = "/org/mpris/MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
SKIP_PLAYERS = ("org.mpris.MediaPlayer2.playerctld",)  # proxy that mirrors another player
NO_TRACK = "/org/mpris/MediaPlayer2/TrackList/NoTrack"
_SIG = re.compile(r"^[ybnqiuxtdsogavh(){}]+$")
_IDENTITIES = {"mozilla firefox": "Firefox", "firefox": "Firefox", "google chrome": "Chrome", "chrome": "Chrome",
               "microsoft edge": "Edge", "vlc media player": "VLC", "vlc": "VLC", "brave": "Brave", "opera": "Opera",
               "spotify": "Spotify", "mpv": "mpv", "mpv media player": "mpv"}  # fmt: skip


# ---- D-Bus -------------------------------------------------------------------------------------------
def unwrap(v: Any) -> Any:
    """jeepney returns variants as (signature, value) tuples: strip them recursively."""
    if isinstance(v, tuple) and len(v) == 2 and isinstance(v[0], str) and _SIG.match(v[0]):
        return unwrap(v[1])
    if isinstance(v, dict):
        return {k: unwrap(x) for k, x in v.items()}
    if isinstance(v, list):
        return [unwrap(x) for x in v]
    return v


class MprisBus(Protocol):
    def names(self) -> list[str]: ...
    def get_all(self, name: str) -> dict[str, Any]: ...
    def identity(self, name: str) -> str | None: ...
    def call(self, name: str, method: str, signature: str = "", args: tuple = ()) -> None: ...


class JeepneyBus:
    """Blocking jeepney connection to the session bus, shared by worker threads (serialized by a lock)."""

    TIMEOUT_S = 1.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conn: Any = None

    def _request(self, msg: Any) -> Any:
        from jeepney.io.blocking import open_dbus_connection
        from jeepney.wrappers import unwrap_msg

        with self._lock:
            try:
                if self._conn is None:
                    self._conn = open_dbus_connection(bus="SESSION")
                return unwrap_msg(self._conn.send_and_get_reply(msg, timeout=self.TIMEOUT_S))
            except Exception as e:
                if not type(e).__name__.startswith("DBusErrorResponse"):
                    self._close()  # broken connection: reconnect next time
                raise

    def _close(self) -> None:
        try:
            if self._conn is not None:
                self._conn.close()
        finally:
            self._conn = None

    @staticmethod
    def _addr(name: str, iface: str = PLAYER_IFACE) -> Any:
        from jeepney import DBusAddress

        return DBusAddress(MPRIS_PATH, bus_name=name, interface=iface)

    def names(self) -> list[str]:
        from jeepney.bus_messages import message_bus

        return [n for n in self._request(message_bus.ListNames())[0] if n.startswith(MPRIS_PREFIX)]

    def get_all(self, name: str) -> dict[str, Any]:
        from jeepney import Properties

        return unwrap(self._request(Properties(self._addr(name)).get_all())[0])

    def identity(self, name: str) -> str | None:
        from jeepney import Properties

        try:
            return str(unwrap(self._request(Properties(self._addr(name, "org.mpris.MediaPlayer2")).get("Identity"))[0]))
        except Exception:
            return None

    def call(self, name: str, method: str, signature: str = "", args: tuple = ()) -> None:
        from jeepney import new_method_call

        self._request(new_method_call(self._addr(name), method, signature or None, args))


# ---- players -------------------------------------------------------------------------------------------
def friendly_identity(identity: str | None, bus_name: str) -> str:
    if identity:
        return _IDENTITIES.get(identity.strip().lower(), identity.strip())[:24]
    base = bus_name.removeprefix(MPRIS_PREFIX).split(".")[0]
    return (_IDENTITIES.get(base.lower()) or base.capitalize())[:24]


@dataclass
class PlayerInfo:
    aumid: str  # D-Bus name (pick_session's player key)
    playing: bool
    title: str
    app: str = ""
    artist: str | None = None
    pos_s: int | None = None
    dur_s: int | None = None
    can_seek: bool = False
    art_url: str | None = None
    track_id: str | None = None
    props: dict[str, Any] = field(default_factory=dict)


def player_info(name: str, props: dict[str, Any], identity: str | None) -> PlayerInfo:
    """MPRIS Player properties (unwrapped) -> PlayerInfo. Times are microseconds in MPRIS."""
    md = props.get("Metadata") or {}
    artist = md.get("xesam:artist")
    if isinstance(artist, (list, tuple)):
        artist = ", ".join(str(a) for a in artist if a) or None
    length = md.get("mpris:length")
    dur = int(length) // 1_000_000 if isinstance(length, (int, float)) and length > 0 else None
    pos_us = props.get("Position")
    pos = int(pos_us) // 1_000_000 if isinstance(pos_us, (int, float)) and pos_us >= 0 else None
    if pos is not None and dur is not None:
        pos = min(pos, dur)
    track = md.get("mpris:trackid")
    return PlayerInfo(
        aumid=name,
        playing=props.get("PlaybackStatus") == "Playing",
        title=str(md.get("xesam:title") or ""),
        app=friendly_identity(identity, name),
        artist=str(artist) if artist else None,
        pos_s=pos if dur is not None else None,
        dur_s=dur,
        can_seek=bool(props.get("CanSeek")) and dur is not None,
        art_url=str(md["mpris:artUrl"]) if md.get("mpris:artUrl") else None,
        track_id=str(track) if track and track != NO_TRACK else None,
        props=props,
    )


class LinuxMedia:
    def __init__(self, bus: MprisBus | None = None, art: ArtFetcher | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.bus: MprisBus = bus or JeepneyBus()
        self._art = art or ArtFetcher()
        self._clock = clock
        self._identities: dict[str, str | None] = {}
        self._last_playing: dict[str, float] = {}
        self._tiles: dict[str, Any] = {}
        self._track_key: tuple | None = None
        # pick_session / hold_track state (same attributes as the Windows backend)
        self._cur: tuple[str, str] | None = None
        self._rival_since: dict[tuple[str, str], float] = {}
        self._manager = None
        self._shown: MediaState | None = None
        self._shown_playing_t = 0.0
        self._chosen: PlayerInfo | None = None
        self.ok = self._probe()

    def _probe(self) -> bool:
        try:
            self.bus.names()
            return True
        except Exception as e:
            log.warning("D-Bus session bus unavailable (no media control): %s", type(e).__name__)
            return False

    def players(self) -> list[PlayerInfo]:
        now = self._clock()
        out: list[PlayerInfo] = []
        for name in self.bus.names():
            if name in SKIP_PLAYERS:
                continue
            try:
                props = self.bus.get_all(name)
            except Exception:
                continue
            if name not in self._identities:
                self._identities[name] = self.bus.identity(name)
            info = player_info(name, props, self._identities[name])
            if info.playing:
                self._last_playing[name] = now
            out.append(info)
        # Most recently playing first: pick_session falls back to the first entry.
        out.sort(key=lambda i: (-self._last_playing.get(i.aumid, 0.0), i.aumid))
        return out

    def _tile(self, svc: Any) -> Any:
        if svc.name not in self._tiles:
            try:
                from .artwork import service_tile

                self._tiles[svc.name] = service_tile(svc)
            except Exception:
                self._tiles[svc.name] = None
        return self._tiles[svc.name]

    def state(self, volume_pct: int | None = None, muted: bool | None = None) -> MediaState:
        st = MediaState(volume_pct=volume_pct, muted=muted)
        try:
            picked = pick_session(self, self.players(), self._clock()) if self.ok else None
        except Exception:
            log.debug("MPRIS read failed", exc_info=True)
            picked = None
        self._chosen = picked
        if picked is not None:
            st.title, st.artist, st.app, st.playing = picked.title or None, picked.artist, picked.app, picked.playing
            st.pos_s, st.dur_s, st.can_seek = picked.pos_s, picked.dur_s, picked.can_seek
            svc = services.detect(st.app, st.title, st.artist)
            if svc is not None:
                st.title = services.clean_title(svc, st.title or "")
                st.app = f"{svc.name} · {st.app}"
            key = (st.app, st.title, st.artist)
            if key != self._track_key:
                self._track_key = key
                self._art.reset()  # browsers reuse one temp file for every cover
            st.art = self._tile(svc) if svc is not None else self._art.get(picked.art_url)
        return hold_track(self, st, self._clock())

    def _target(self) -> PlayerInfo | None:
        if self._chosen is not None:
            return self._chosen
        players = self.players()
        return pick_session(self, players, self._clock()) if players else None

    def command(self, op: str) -> bool:
        method = {"play_pause": "PlayPause", "next": "Next", "previous": "Previous"}[op]
        p = self._target()
        if p is None:
            return False
        self.bus.call(p.aumid, method)
        return True

    def seek(self, pos_s: int) -> bool:
        p = self._target()
        if p is None or not p.can_seek:
            return False
        pos = max(0, min(pos_s, p.dur_s)) if p.dur_s else max(0, pos_s)
        if p.track_id:
            self.bus.call(p.aumid, "SetPosition", "ox", (p.track_id, pos * 1_000_000))
        else:
            now = player_info(p.aumid, self.bus.get_all(p.aumid), p.app).pos_s or 0
            self.bus.call(p.aumid, "Seek", "x", ((pos - now) * 1_000_000,))
        return True


# ---- volume ----------------------------------------------------------------------------------------
def run_out(cmd: list[str]) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def parse_wpctl_volume(text: str) -> tuple[int | None, bool | None]:
    """'Volume: 0.40 [MUTED]' -> (40, True). Values above 1.0 (software boost) are capped at 100."""
    m = re.search(r"Volume:\s*([\d.]+)", text or "")
    if not m:
        return None, None
    return min(100, round(float(m.group(1)) * 100)), "[MUTED]" in text


def parse_pactl_volume(text: str) -> int | None:
    """'Volume: front-left: 26214 /  40% / -23.88 dB,   front-right: ... /  40% / ...' -> 40 (channel mean)."""
    vals = [int(v) for v in re.findall(r"(\d+)%", text or "")]
    return min(100, round(sum(vals) / len(vals))) if vals else None


def parse_pactl_mute(text: str) -> bool | None:
    m = re.search(r"Mute:\s*(yes|no)", text or "")
    return None if not m else m.group(1) == "yes"


class LinuxVolume:
    """Default output volume via PipeWire (wpctl) or PulseAudio (pactl, also pipewire-pulse)."""

    def __init__(self, run: Callable[[list[str]], str | None] = run_out, which: Callable[[str], str | None] = shutil.which) -> None:
        self._run = run
        self.tool: str | None = None
        if which("wpctl") and run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"]) is not None:
            self.tool = "wpctl"
        elif which("pactl") and run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"]) is not None:
            self.tool = "pactl"
        log.info("volume control: %s", self.tool or "unavailable (no wpctl / pactl)")

    @property
    def ok(self) -> bool:
        return self.tool is not None

    def get(self) -> tuple[int | None, bool | None]:
        if self.tool == "wpctl":
            return parse_wpctl_volume(self._run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"]) or "")
        if self.tool == "pactl":
            return (parse_pactl_volume(self._run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"]) or ""),
                    parse_pactl_mute(self._run(["pactl", "get-sink-mute", "@DEFAULT_SINK@"]) or ""))  # fmt: skip
        return None, None

    def set(self, pct: int) -> int:
        pct = max(0, min(100, pct))
        if self.tool == "wpctl":
            self._run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{pct}%"])
            if pct > 0:
                self._run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"])  # raising the volume implies unmute
        elif self.tool == "pactl":
            self._run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{pct}%"])
            if pct > 0:
                self._run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"])
        else:
            raise RuntimeError("no volume control")
        got, _ = self.get()
        return pct if got is None else got


# ---- backend --------------------------------------------------------------------------------------
class LinuxBackend:
    def __init__(
        self,
        allow_power: Callable[[], bool] = lambda: False,
        *,
        media: LinuxMedia | None = None,
        volume: LinuxVolume | None = None,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self.allow_power = allow_power
        self.volume = volume or LinuxVolume()
        self.media_backend = media or LinuxMedia()
        self._which = which

    def set_volume(self, pct: int) -> int:
        return self.volume.set(pct)

    def media(self, op: str) -> bool:
        return self.media_backend.command(op)

    def seek(self, pos_s: int) -> bool:
        return self.media_backend.seek(pos_s)

    def lock(self) -> bool:
        return lock_linux()

    def power(self, op: str) -> bool:
        return schedule_power("linux", op)

    def state(self) -> MediaState:
        vol, muted = self.volume.get() if self.volume.ok else (None, None)
        return self.media_backend.state(vol, muted)

    def available(self) -> tuple[str, ...]:
        acts: list[str] = []
        if self.volume.ok:
            acts.append("volume.set")
        if self.media_backend.ok:
            acts += ["media.play_pause", "media.next", "media.previous", "media.seek"]
        if self._which("loginctl") or self._which("xdg-screensaver"):
            acts.append("session.lock")
        if self.allow_power() and self._which("systemctl"):
            acts += ["system.shutdown", "system.restart"]
        return tuple(acts)
