"""macOS media / volume / session backend (AppleScript through ``osascript``; no pyobjc).

- Players: **Music** and **Spotify** (the apps with an AppleScript dictionary). macOS has no public
  API for the system-wide "Now Playing" (MediaRemote is private and locked down since macOS 15.4), so
  browsers and other players are not shown. Scripts only talk to apps that are installed *and*
  running (``application "X" is running`` never launches them). The first call makes macOS ask for
  the Automation permission ("Control Deck wants to control Music / Spotify").
- One ``osascript`` per sample returns the output volume, mute and both players' state.
- Artwork: Spotify's ``artwork url`` (downloaded like on Linux); Music's ``raw data`` of artwork 1,
  written to a private temp file on track changes.
- Volume: ``set volume output volume N`` (``missing value`` = an output without volume control).
- Lock / power: ``posix_session``.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .art_fetch import ArtFetcher
from .media_common import MediaState, hold_track, pick_session
from .posix_session import lock_macos, schedule_power

log = logging.getLogger(__name__)

SEP = "\x1f"  # ASCII unit separator: never part of a title
APPS = {
    "Spotify": ("/Applications/Spotify.app", "~/Applications/Spotify.app"),
    "Music": ("/System/Applications/Music.app", "/Applications/Music.app"),
}
_PLAYER_BLOCK = {
    "Spotify": """
if application "Spotify" is running then
  tell application "Spotify"
    try
      set st to (player state as text)
      set t to current track
      set out to out & "Spotify" & sep & st & sep & (name of t) & sep & (artist of t) & sep & ((player position) div 1) & sep & ((duration of t) div 1000) & sep & (artwork url of t) & sep & (id of t) & linefeed
    on error
      set out to out & "Spotify" & sep & "stopped" & linefeed
    end try
  end tell
end if""",
    "Music": """
if application "Music" is running then
  tell application "Music"
    try
      set st to (player state as text)
      set t to current track
      set out to out & "Music" & sep & st & sep & (name of t) & sep & (artist of t) & sep & ((player position) div 1) & sep & ((duration of t) div 1) & sep & "" & sep & (persistent ID of t) & linefeed
    on error
      set out to out & "Music" & sep & "stopped" & linefeed
    end try
  end tell
end if""",
}
_VOLUME = """set vs to (get volume settings)
set ov to output volume of vs
if ov is missing value then set ov to "none"
set om to output muted of vs
if om is missing value then set om to "none"
set out to "VOL" & sep & (ov as text) & sep & (om as text) & linefeed"""
_COMMANDS = {"play_pause": "playpause", "next": "next track", "previous": "previous track"}


def state_script(apps: list[str]) -> str:
    return "set sep to (character id 31)\nset out to \"\"\n" + _VOLUME + "".join(_PLAYER_BLOCK[a] for a in apps) + "\nreturn out\n"


@dataclass
class MacPlayer:
    aumid: str  # "Spotify" | "Music"
    playing: bool
    title: str
    artist: str | None = None
    pos_s: int | None = None
    dur_s: int | None = None
    art_url: str | None = None
    track_id: str | None = None


def parse_state(text: str) -> tuple[int | None, bool | None, list[MacPlayer]]:
    """osascript output -> (volume %, muted, players with a current track)."""
    vol: int | None = None
    muted: bool | None = None
    players: list[MacPlayer] = []
    for line in (text or "").splitlines():
        f = line.split(SEP)
        if f[0] == "VOL" and len(f) >= 3:
            vol = int(f[1]) if f[1].strip().isdigit() else None
            muted = {"true": True, "false": False}.get(f[2].strip())
        elif f[0] in APPS and len(f) >= 8 and f[1] != "stopped":

            def num(s: str) -> int | None:
                return int(s) if s.strip().lstrip("-").isdigit() and int(s) >= 0 else None

            dur = num(f[5])
            players.append(MacPlayer(
                aumid=f[0], playing=f[1] == "playing", title=f[2], artist=f[3] or None,
                pos_s=num(f[4]) if dur else None, dur_s=dur or None,
                art_url=f[6] or None, track_id=f[7] or None,
            ))  # fmt: skip
    return vol, muted, players


def run_osascript(script: str, timeout: float = 4.0) -> str | None:
    try:
        r = subprocess.run(["osascript", "-"], input=script, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        log.debug("osascript failed: %s", r.stderr.strip()[:200])
        return None
    return r.stdout


def installed_apps(exists: Callable[[str], bool] = lambda p: Path(os.path.expanduser(p)).exists()) -> list[str]:
    return [app for app, paths in APPS.items() if any(exists(p) for p in paths)]


class MacMedia:
    def __init__(self, run: Callable[[str], str | None] = run_osascript, apps: list[str] | None = None,
                 art: ArtFetcher | None = None, clock: Callable[[], float] = time.monotonic) -> None:  # fmt: skip
        self._run = run
        self.apps = installed_apps() if apps is None else apps
        self._art = art or ArtFetcher()
        self._clock = clock
        self._script = state_script(self.apps)
        self._track: tuple | None = None
        self._music_art: Any = None
        self._tmp = Path(tempfile.mkdtemp(prefix="pcdeck-art-"))
        self._cur: tuple[str, str] | None = None
        self._rival_since: dict[tuple[str, str], float] = {}
        self._manager = None
        self._shown: MediaState | None = None
        self._shown_playing_t = 0.0
        self._chosen: MacPlayer | None = None
        self.volume_ok = False
        log.info("macOS media: players %s", ", ".join(self.apps) or "none installed (Music / Spotify)")

    def read(self) -> tuple[int | None, bool | None, list[MacPlayer]]:
        vol, muted, players = parse_state(self._run(self._script) or "")
        self.volume_ok = vol is not None
        players.sort(key=lambda p: not p.playing)
        return vol, muted, players

    def _music_artwork(self) -> Any:
        path = self._tmp / "music-cover"
        script = f"""tell application "Music"
  try
    set d to raw data of artwork 1 of current track
  on error
    return ""
  end try
end tell
set f to open for access (POSIX file "{path}") with write permission
set eof f to 0
write d to f
close access f
return "ok"
"""
        if (self._run(script) or "").strip() != "ok":
            return None
        try:
            from .artwork import process

            return process(path.read_bytes())
        except Exception:
            return None

    def state(self) -> MediaState:
        vol, muted, players = self.read()
        st = MediaState(volume_pct=vol, muted=muted)
        picked = pick_session(self, players, self._clock())
        self._chosen = picked
        if picked is not None:
            st.title, st.artist, st.app, st.playing = picked.title or None, picked.artist, picked.aumid, picked.playing
            st.pos_s, st.dur_s, st.can_seek = picked.pos_s, picked.dur_s, picked.dur_s is not None
            key = (picked.aumid, picked.track_id, picked.title)
            if picked.aumid == "Music":
                if key != self._track:
                    self._music_art = self._music_artwork()
                st.art = self._music_art
            else:
                st.art = self._art.get(picked.art_url)
            self._track = key
        return hold_track(self, st, self._clock())

    def _target(self) -> MacPlayer | None:
        if self._chosen is None:
            _, _, players = self.read()
            self._chosen = pick_session(self, players, self._clock())
        return self._chosen

    def command(self, op: str) -> bool:
        p = self._target()
        if p is None or p.aumid not in APPS:
            return False
        return self._run(f'tell application "{p.aumid}" to {_COMMANDS[op]}') is not None

    def seek(self, pos_s: int) -> bool:
        p = self._target()
        if p is None or p.aumid not in APPS or not p.dur_s:
            return False
        pos = max(0, min(int(pos_s), p.dur_s))
        return self._run(f'tell application "{p.aumid}" to set player position to {pos}') is not None

    def set_volume(self, pct: int) -> int:
        pct = max(0, min(100, int(pct)))
        script = f"set volume output volume {pct}" + ("\nset volume without output muted" if pct > 0 else "")
        if self._run(script) is None:
            raise RuntimeError("set volume failed")
        vol, _, _ = parse_state(self._run(state_script([])) or "")
        return pct if vol is None else vol


class MacBackend:
    def __init__(self, allow_power: Callable[[], bool] = lambda: False, *, media: MacMedia | None = None) -> None:
        self.allow_power = allow_power
        self.media_backend = media or MacMedia()
        self.media_backend.read()  # learns whether the output has a volume control

    def set_volume(self, pct: int) -> int:
        return self.media_backend.set_volume(pct)

    def media(self, op: str) -> bool:
        return self.media_backend.command(op)

    def seek(self, pos_s: int) -> bool:
        return self.media_backend.seek(pos_s)

    def lock(self) -> bool:
        return lock_macos()

    def power(self, op: str) -> bool:
        return schedule_power("macos", op)

    def state(self) -> MediaState:
        return self.media_backend.state()

    def available(self) -> tuple[str, ...]:
        acts: list[str] = ["volume.set"] if self.media_backend.volume_ok else []
        if self.media_backend.apps:
            acts += ["media.play_pause", "media.next", "media.previous", "media.seek"]
        acts.append("session.lock")
        if self.allow_power():
            acts += ["system.shutdown", "system.restart"]
        return tuple(acts)
