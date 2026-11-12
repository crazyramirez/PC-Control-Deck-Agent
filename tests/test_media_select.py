"""Sticky media-session selection and the track-change grace hold (pure logic, no WinRT)."""

from dataclasses import dataclass
from types import SimpleNamespace

from pcdeck_agent.commands.windows_media import GRACE_S, SWITCH_AFTER_S, MediaState, hold_track, pick_session


@dataclass
class S:
    aumid: str
    playing: bool
    title: str


def model():
    return SimpleNamespace(_cur=None, _rival_since={}, _manager=None, _shown=None, _shown_playing_t=0.0)


def test_shown_player_keeps_focus_until_rival_plays_long_enough():
    m = model()
    spoty = S("Chrome", True, "Song A")
    assert pick_session(m, [spoty], 0.0) is spoty
    # Skip: Chrome pauses for a moment while WhatsApp starts playing.
    paused = S("Chrome", False, "Song A")
    wa = S("WhatsApp", True, "Voice note")
    assert pick_session(m, [paused, wa], 1.0) is paused
    assert pick_session(m, [paused, wa], 1.0 + SWITCH_AFTER_S - 0.1) is paused
    # WhatsApp kept playing long enough: it takes over.
    assert pick_session(m, [paused, wa], 1.0 + SWITCH_AFTER_S) is wa


def test_new_track_in_same_player_is_followed_at_once():
    m = model()
    pick_session(m, [S("Spotify", True, "A")], 0.0)
    nxt = S("Spotify", True, "B")
    assert pick_session(m, [nxt, S("Chrome", True, "video")], 1.0) is nxt
    assert m._cur == ("Spotify", "B")


def test_player_gone_follows_what_plays():
    m = model()
    pick_session(m, [S("Spotify", True, "A")], 0.0)
    yt = S("Chrome", True, "video")
    assert pick_session(m, [yt], 1.0) is yt


def test_grace_hold_on_track_change():
    m = model()
    a = MediaState(volume_pct=20, title="A", app="Chrome", playing=True)
    assert hold_track(m, a, 0.0) is a
    gap = MediaState(volume_pct=25, title=None, app="Chrome", playing=False)
    held = hold_track(m, gap, 1.0)
    assert held.title == "A" and held.volume_pct == 25
    b = MediaState(volume_pct=25, title="B", app="Chrome", playing=True)
    assert hold_track(m, b, 2.0) is b
    # A real pause of the same track is shown immediately.
    p = MediaState(volume_pct=25, title="B", app="Chrome", playing=False)
    assert hold_track(m, p, 3.0) is p
    # A change while paused long ago is not held.
    other = MediaState(volume_pct=25, title="C", app="Chrome", playing=False)
    assert hold_track(m, other, 3.0 + GRACE_S) is other
