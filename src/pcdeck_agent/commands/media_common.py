"""OS-independent media pieces shared by the Windows, Linux and macOS backends.

- ``MediaState``: what the deck shows (volume, now playing, position, artwork);
- ``pick_session``: sticky player choice -- the player the deck shows keeps the focus; another one
  takes over only after playing for SWITCH_AFTER_S (or at once if the current player is gone);
- ``hold_track``: GRACE_S hold so a transient pause / empty title between tracks never flashes.
Moved verbatim from windows_media.py (which re-exports them) when Linux/macOS backends were added.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SWITCH_AFTER_S = 3.0  # another player must play this long before it replaces the shown one
GRACE_S = 4.0  # hold the shown track while the player changes tracks


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
