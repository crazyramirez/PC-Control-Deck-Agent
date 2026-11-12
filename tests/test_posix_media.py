"""Linux (MPRIS / wpctl / pactl) and macOS (osascript) media backends, lock and power, with fakes."""

import io

import pytest
from PIL import Image

from pcdeck_agent.commands import linux_media as LM
from pcdeck_agent.commands import macos_media as MM
from pcdeck_agent.commands import posix_session as PS
from pcdeck_agent.commands.art_fetch import ArtFetcher, load_art_bytes
from pcdeck_agent.commands.linux_media import LinuxBackend, LinuxMedia, LinuxVolume, player_info, unwrap
from pcdeck_agent.commands.macos_media import MacBackend, MacMedia, parse_state, state_script
from pcdeck_agent.commands.media_common import SWITCH_AFTER_S
from pcdeck_agent.commands.router import CommandRouter


def png(color=(0, 0, 255)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(buf, "PNG")
    return buf.getvalue()


def mpris(status, title, artist=("Artist",), length_us=215_000_000, pos_us=83_500_000, art=None, trackid="/org/mpris/track/1"):
    """Player properties exactly as jeepney returns them: variants are (signature, value) tuples."""
    md = {"mpris:trackid": ("o", trackid), "mpris:length": ("x", length_us), "xesam:title": ("s", title),
          "xesam:artist": ("as", list(artist))}  # fmt: skip
    if art:
        md["mpris:artUrl"] = ("s", art)
    return {"PlaybackStatus": ("s", status), "Position": ("x", pos_us), "CanSeek": ("b", True), "Metadata": ("a{sv}", md)}


class FakeBus:
    def __init__(self, players: dict, identities: dict | None = None):
        self.players = players
        self.identities = identities or {}
        self.calls = []

    def names(self):
        return ["org.freedesktop.Notifications", *self.players]  # the real bus lists everything

    def get_all(self, name):
        return unwrap(self.players[name])

    def identity(self, name):
        return self.identities.get(name)

    def call(self, name, method, signature="", args=()):
        self.calls.append((name, method, signature, args))


def test_unwrap_and_player_mapping():
    props = unwrap(mpris("Playing", "Song", ("A", "B"), art="file:///tmp/cover.png"))
    assert props["Metadata"]["xesam:artist"] == ["A", "B"] and props["Position"] == 83_500_000
    p = player_info("org.mpris.MediaPlayer2.spotify", props, "Spotify")
    assert (p.title, p.artist, p.app, p.playing, p.pos_s, p.dur_s, p.can_seek) == ("Song", "A, B", "Spotify", True, 83, 215, True)
    assert (p.art_url, p.track_id) == ("file:///tmp/cover.png", "/org/mpris/track/1")
    live = player_info("org.mpris.MediaPlayer2.firefox.instance_1_7", {"PlaybackStatus": "Paused", "Metadata": {"xesam:title": "Live"}}, "Mozilla Firefox")
    assert (live.app, live.dur_s, live.pos_s, live.can_seek, live.artist) == ("Firefox", None, None, False, None)
    assert LM.friendly_identity(None, "org.mpris.MediaPlayer2.vlc") == "VLC"


def test_mpris_state_selection_and_commands():
    t = [0.0]
    bus = FakeBus({"org.mpris.MediaPlayer2.spotify": mpris("Playing", "Song A"),
                   "org.mpris.MediaPlayer2.playerctld": mpris("Playing", "Song A")},  # proxy: ignored
                  {"org.mpris.MediaPlayer2.spotify": "Spotify"})  # fmt: skip
    m = LinuxMedia(bus, art=ArtFetcher(loader=lambda u: None, background=False), clock=lambda: t[0])
    st = m.state(volume_pct=40, muted=False)
    assert (st.title, st.artist, st.app, st.playing, st.pos_s, st.dur_s, st.can_seek, st.volume_pct) == \
        ("Song A", "Artist", "Spotify", True, 83, 215, True, 40)  # fmt: skip
    # A browser starts playing: the shown player keeps the focus until the rival plays long enough.
    bus.players["org.mpris.MediaPlayer2.spotify"] = mpris("Paused", "Song A")
    bus.players["org.mpris.MediaPlayer2.chromium.instance42"] = mpris("Playing", "Clip", ("Channel",))
    t[0] = 1.0
    assert m.state().app == "Spotify"
    t[0] = 1.0 + SWITCH_AFTER_S
    assert m.state().title == "Clip"
    assert m.command("play_pause") and bus.calls[-1][:2] == ("org.mpris.MediaPlayer2.chromium.instance42", "PlayPause")
    assert m.seek(500) and bus.calls[-1] == ("org.mpris.MediaPlayer2.chromium.instance42", "SetPosition", "ox", ("/org/mpris/track/1", 215_000_000))


def test_mpris_seek_without_trackid_and_no_player():
    bus = FakeBus({"org.mpris.MediaPlayer2.mpv": mpris("Playing", "x", trackid="/org/mpris/MediaPlayer2/TrackList/NoTrack", pos_us=10_000_000)})
    m = LinuxMedia(bus, art=ArtFetcher(background=False), clock=lambda: 0.0)
    m.state()
    assert m.seek(30) and bus.calls[-1] == ("org.mpris.MediaPlayer2.mpv", "Seek", "x", (20_000_000,))
    empty = LinuxMedia(FakeBus({}), clock=lambda: 0.0)
    assert empty.state().title is None and not empty.command("next") and not empty.seek(5)


def test_mpris_artwork_and_service_tile(tmp_path):
    cover = tmp_path / "cover.png"
    cover.write_bytes(png())
    url = cover.as_uri()
    assert load_art_bytes(url) == cover.read_bytes() and load_art_bytes("ftp://x/y.png") is None
    bus = FakeBus({"org.mpris.MediaPlayer2.rhythmbox": mpris("Playing", "Track", art=url)}, {"org.mpris.MediaPlayer2.rhythmbox": "Rhythmbox"})
    st = LinuxMedia(bus, art=ArtFetcher(background=False), clock=lambda: 0.0).state()
    assert st.art is not None and st.art.width == 150 and st.art.rgb565be[:2] == bytes([0x00, 0x1F])
    yt = FakeBus({"org.mpris.MediaPlayer2.firefox.instance_1_9": mpris("Playing", "Lo-fi beats - YouTube", artist=())},
                 {"org.mpris.MediaPlayer2.firefox.instance_1_9": "Mozilla Firefox"})  # fmt: skip
    st = LinuxMedia(yt, art=ArtFetcher(background=False), clock=lambda: 0.0).state()
    assert (st.title, st.app) == ("Lo-fi beats", "YouTube · Firefox") and st.art is not None


def test_volume_parsers_and_control():
    assert LM.parse_wpctl_volume("Volume: 0.40\n") == (40, False)
    assert LM.parse_wpctl_volume("Volume: 1.25 [MUTED]\n") == (100, True)
    assert LM.parse_wpctl_volume("") == (None, None)
    assert LM.parse_pactl_volume("Volume: front-left: 26214 /  40% / -23.88 dB,   front-right: 28180 /  43% / -21.99 dB\n        balance 0.00") == 42
    assert LM.parse_pactl_mute("Mute: yes") is True and LM.parse_pactl_mute("") is None

    state = {"vol": "0.30", "muted": True}
    calls = []

    def run(cmd):
        calls.append(cmd)
        if cmd[1] == "get-volume":
            return f"Volume: {state['vol']}{' [MUTED]' if state['muted'] else ''}\n"
        if cmd[1] == "set-volume":
            state["vol"] = f"{int(cmd[3].rstrip('%')) / 100:.2f}"
        if cmd[1] == "set-mute":
            state["muted"] = cmd[3] == "1"
        return ""

    v = LinuxVolume(run=run, which=lambda tool: tool == "wpctl")
    assert v.tool == "wpctl" and v.get() == (30, True)
    assert v.set(65) == 65 and state["muted"] is False  # raising the volume unmutes, like Windows
    assert LinuxVolume(run=lambda cmd: None, which=lambda tool: None).ok is False


def test_linux_backend_actions_through_router():
    bus = FakeBus({"org.mpris.MediaPlayer2.spotify": mpris("Playing", "Song")})
    vol = LinuxVolume(run=lambda cmd: "Volume: 0.50\n", which=lambda tool: tool == "wpctl")
    allowed = {"on": False}
    tools = {"wpctl", "loginctl", "systemctl"}
    b = LinuxBackend(lambda: allowed["on"], media=LinuxMedia(bus, clock=lambda: 0.0), volume=vol, which=lambda t: t if t in tools else None)
    assert b.available() == ("volume.set", "media.play_pause", "media.next", "media.previous", "media.seek", "session.lock")
    allowed["on"] = True
    assert b.available()[-2:] == ("system.shutdown", "system.restart")
    r = CommandRouter(b)
    assert r.execute("d", "1", {"action": "media.next"}).ok and bus.calls[-1][1] == "Next"
    assert b.state().volume_pct == 50
    tools.discard("systemctl")
    assert "system.shutdown" not in b.available()


# ---- macOS ---------------------------------------------------------------------------------------
SEP = "\x1f"


def osa_output(*players, vol="35", muted="false"):
    lines = [SEP.join(["VOL", vol, muted])] + [SEP.join(p) for p in players]
    return "\n".join(lines) + "\n"


def test_parse_osascript_state():
    out = osa_output(("Spotify", "playing", "Song", "Band", "61", "200", "https://i.scdn.co/image/ab", "spotify:track:1"),
                     ("Music", "stopped"))  # fmt: skip
    vol, muted, players = parse_state(out)
    assert (vol, muted) == (35, False) and len(players) == 1
    p = players[0]
    assert (p.aumid, p.playing, p.title, p.artist, p.pos_s, p.dur_s, p.art_url) == ("Spotify", True, "Song", "Band", 61, 200, "https://i.scdn.co/image/ab")
    assert parse_state(osa_output(vol="none", muted="none"))[:2] == (None, None)  # output without volume control
    assert parse_state("") == (None, None, [])
    script = state_script(["Music"])
    assert 'application "Music" is running' in script and "Spotify" not in script and "get volume settings" in script


def test_mac_media_state_commands_and_volume():
    scripts = []
    state = {"out": osa_output(("Music", "paused", "Tune", "", "5", "180", "", "ABC123"),
                               ("Spotify", "playing", "Song", "Band", "61", "200", "", "spotify:track:1"))}  # fmt: skip

    def run(script):
        scripts.append(script)
        if script.startswith("set sep"):
            return state["out"]
        if "raw data of artwork" in script:
            return ""  # no artwork
        return ""

    m = MacMedia(run=run, apps=["Spotify", "Music"], art=ArtFetcher(background=False), clock=lambda: 0.0)
    st = m.state()
    assert (st.app, st.title, st.playing, st.pos_s, st.dur_s, st.can_seek, st.volume_pct) == ("Spotify", "Song", True, 61, 200, True, 35)
    assert m.command("next") and scripts[-1] == 'tell application "Spotify" to next track'
    assert m.seek(999) and scripts[-1] == 'tell application "Spotify" to set player position to 200'
    assert m.set_volume(60) == 35  # read back from the (fake) system
    assert "set volume output volume 60" in scripts[-2] and "without output muted" in scripts[-2]
    b = MacBackend(lambda: False, media=m)
    assert b.available() == ("volume.set", "media.play_pause", "media.next", "media.previous", "media.seek", "session.lock")
    none = MacMedia(run=lambda s: osa_output(vol="none"), apps=[], clock=lambda: 0.0)
    assert MacBackend(media=none).available() == ("session.lock",) and not none.command("play_pause")


# ---- lock / power --------------------------------------------------------------------------------
def test_lock_fallbacks():
    ran = []
    assert PS.lock_linux(run=lambda c: ran.append(c) or True, which=lambda t: t)
    assert ran == [["loginctl", "lock-session"]]
    assert PS.lock_linux(run=lambda c: c[0] == "xdg-screensaver", which=lambda t: t)
    assert not PS.lock_linux(run=lambda c: True, which=lambda t: None)
    assert PS.lock_macos(run=lambda c: False, sac=lambda: True)
    ran.clear()
    assert PS.lock_macos(run=lambda c: ran.append(c) or True, sac=lambda: False) and ran == [["pmset", "displaysleepnow"]]


@pytest.mark.parametrize("os_name,cmd", [("linux", ["systemctl", "poweroff"]), ("macos", ["osascript", "-e", 'tell application "System Events" to shut down'])])
def test_power_is_delayed_with_notice(monkeypatch, os_name, cmd):
    monkeypatch.setattr(PS.shutil, "which", lambda t: f"/usr/bin/{t}")
    ran, timers = [], []
    assert PS.schedule_power(os_name, "shutdown", run=lambda c: ran.append(c) or True, start=timers.append)
    assert len(ran) == 1 and ran[0][0] in ("notify-send", "osascript") and cmd not in ran  # only the notice so far
    assert timers[0].interval == PS.POWER_DELAY_S and timers[0].daemon
    timers[0].function()
    assert ran[-1] == cmd
    restart, _ = PS.power_commands(os_name, "restart")
    assert restart[-1].endswith("reboot") or restart[-1].endswith("restart")
    monkeypatch.setattr(PS.shutil, "which", lambda t: None)
    assert not PS.schedule_power(os_name, "shutdown", run=lambda c: True, start=timers.append)
    assert not PS.schedule_power(os_name, "format", run=lambda c: True, start=timers.append)
