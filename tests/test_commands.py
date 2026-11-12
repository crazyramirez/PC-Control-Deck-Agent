import asyncio
import json

import pytest
from websockets.asyncio.client import connect

from pcdeck_agent import protocol as P
from pcdeck_agent.commands.router import ACTIONS, CommandRouter
from pcdeck_agent.commands.windows_media import MediaState, friendly_app
from pcdeck_agent.hardware.demo_frames import demo_provider
from pcdeck_agent.security.credentials import CredentialStore
from pcdeck_agent.security.pairing import PairingManager
from pcdeck_agent.server import AgentServer


class FakeBackend:
    def __init__(self):
        self.calls = []
        self.volume = 30

    def set_volume(self, pct):
        self.calls.append(("vol", pct))
        self.volume = pct
        return pct

    def media(self, op):
        self.calls.append(("media", op))
        return True

    def seek(self, pos_s):
        self.calls.append(("seek", pos_s))
        return True

    def lock(self):
        self.calls.append(("lock",))
        return True

    def power(self, op):
        self.calls.append(("power", op))
        return True

    def state(self):
        return MediaState(volume_pct=self.volume, muted=False, title="Song", artist="Band", app="Chrome", playing=True)

    def available(self):
        return ("volume.set", "media.play_pause", "media.next", "media.previous", "media.seek", "session.lock")


def test_allowlist_and_validation():
    b = FakeBackend()
    r = CommandRouter(b)
    assert r.execute("d", "1", {"action": "volume.set", "value": 65}).payload() == {"ok": True, "action": "volume.set", "appliedValue": 65}
    for bad in (101, -1, "50", 50.5, True, None):
        assert r.execute("d", f"v{bad}", {"action": "volume.set", "value": bad}).error == "invalid_value"
    assert r.execute("d", "p1", {"action": "system.shutdown"}).error == "not_supported"  # not enabled
    for evil in ("shell.execute", "powershell", "app.launch", "", None, 5):
        assert r.execute("d", f"e{evil}", {"action": evil}).error == "unknown_action"
    assert all(c[0] != "shell" for c in b.calls)
    assert set(ACTIONS) == {"volume.set", "media.play_pause", "media.next", "media.previous", "media.seek", "session.lock", "system.shutdown", "system.restart"}


def test_power_only_when_enabled():
    b = FakeBackend()
    allowed = {"on": False}
    b.available = lambda: ("session.lock",) + (("system.shutdown", "system.restart") if allowed["on"] else ())
    r = CommandRouter(b)
    assert r.execute("d", "1", {"action": "system.restart"}).error == "not_supported"
    allowed["on"] = True
    assert r.execute("d", "2", {"action": "system.restart"}).ok
    assert b.calls == [("power", "restart")]


def test_artwork_processing_and_chunks():
    import io
    import struct

    from PIL import Image

    from pcdeck_agent.commands.artwork import ART_SIZE, CHUNK, HEADER, chunks, process

    img = Image.new("RGB", (300, 200), (255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    art = process(buf.getvalue())
    assert (art.width, art.height) == (ART_SIZE, ART_SIZE) and len(art.rgb565be) == ART_SIZE * ART_SIZE * 2
    assert art.rgb565be[:2] == bytes([0xF8, 0x00])  # pure red, RGB565 big-endian
    assert art.art_id != 0 and art.accent_hex.startswith("#")
    frames = chunks(art)
    assert all(len(f) <= HEADER.size + CHUNK for f in frames)
    magic, aid, w, h, off, total = HEADER.unpack(frames[-1][: HEADER.size])
    assert magic == b"PCDA" and aid == art.art_id and total == len(art.rgb565be) and off + len(frames[-1]) - HEADER.size == total


def test_media_and_lock_dispatch():
    b = FakeBackend()
    r = CommandRouter(b)
    for i, a in enumerate(("media.play_pause", "media.next", "media.previous", "session.lock")):
        assert r.execute("d", f"m{i}", {"action": a}).ok
    assert b.calls == [("media", "play_pause"), ("media", "next"), ("media", "previous"), ("lock",)]


def test_duplicate_request_id_not_reexecuted():
    b = FakeBackend()
    r = CommandRouter(b)
    first = r.execute("d", "x-1", {"action": "session.lock"})
    again = r.execute("d", "x-1", {"action": "session.lock"})
    assert first is again and b.calls == [("lock",)]
    r.execute("other-device", "x-1", {"action": "session.lock"})  # ids are per device
    assert len(b.calls) == 2


def test_rate_limit():
    r = CommandRouter(FakeBackend())
    results = [r.execute("d", f"r{i}", {"action": "media.next"}) for i in range(20)]
    assert any(o.error == "rate_limited" for o in results)


def test_no_backend_means_not_supported():
    r = CommandRouter(None)
    assert r.available_actions() == []
    assert r.execute("d", "1", {"action": "media.next"}).error == "not_supported"


@pytest.mark.parametrize(
    "aumid,name",
    [("Spotify.exe", "Spotify"), ("Chrome", "Chrome"), ("MSEdge", "Edge"), ("5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App", "WhatsApp"), (None, None), ("C:\\x\\foo.exe", "foo")],
)
def test_friendly_app(aumid, name):
    assert friendly_app(aumid) == name


def test_snapshot_carries_audio_and_media():
    from pcdeck_agent.hardware.models import TelemetrySnapshot

    p = P.snapshot_payload(TelemetrySnapshot(host="h"), MediaState(volume_pct=40, muted=True, title="T" * 80, artist="A", app="Spotify", playing=False))
    assert p["audio"] == {"volumePct": 40, "muted": True}
    assert p["media"]["app"] == "Spotify" and len(p["media"]["title"]) <= 47 and p["media"]["playing"] is False
    p2 = P.snapshot_payload(TelemetrySnapshot(host="h"))
    assert p2["audio"] == {"volumePct": None, "muted": None} and p2["media"] is None


def test_end_to_end_command_after_auth(tmp_path):
    async def body():
        creds = CredentialStore(tmp_path / "d.dat", encrypt=False)
        token = creds.add("deck-x", "Mesa")
        backend = FakeBackend()
        srv = AgentServer(demo_provider(), hosts=["127.0.0.1"], port=0, device_id="pc", interval_s=0.5,
                          credentials=creds, pairing=PairingManager(), commands=CommandRouter(backend))  # fmt: skip
        stop = asyncio.Event()
        task = asyncio.create_task(srv.run(stop))
        await srv.wait_ready()
        try:
            async with connect(f"ws://127.0.0.1:{srv.bound[0][1]}") as ws:
                await ws.send(json.dumps({"v": 1, "type": "hello", "payload": {"clientId": "deck-x"}}))
                await ws.recv()
                await ws.send(json.dumps({"v": 1, "type": "auth.request", "payload": {"token": token}}))
                caps = None
                for _ in range(6):
                    m = json.loads(await ws.recv())
                    if m["type"] == P.T_CAPABILITIES and m["payload"]["authenticated"]:
                        caps = m
                        break
                assert caps and "volume.set" in caps["payload"]["actions"]
                await ws.send(json.dumps({"v": 1, "type": "command.request", "requestId": "ab12-7", "payload": {"action": "volume.set", "value": 70}}))
                for _ in range(10):
                    m = json.loads(await ws.recv())
                    if m["type"] == P.T_COMMAND_RESULT:
                        assert m["requestId"] == "ab12-7"
                        assert m["payload"] == {"ok": True, "action": "volume.set", "appliedValue": 70}
                        break
                    if m["type"] == P.T_SNAPSHOT:
                        assert "audio" in m["payload"]
                else:
                    raise AssertionError("no command.result")
            assert ("vol", 70) in backend.calls
        finally:
            stop.set()
            await task

    asyncio.run(asyncio.wait_for(body(), 20))


def test_seek_validation_and_dispatch():
    b = FakeBackend()
    r = CommandRouter(b)
    assert r.execute("d", "s1", {"action": "media.seek", "value": 95}).payload() == {"ok": True, "action": "media.seek", "appliedValue": 95}
    assert r.execute("d", "s2", {"action": "media.seek", "value": -1}).error == "invalid_value"
    assert r.execute("d", "s3", {"action": "media.seek", "value": "10"}).error == "invalid_value"
    assert r.execute("d", "s4", {"action": "media.seek", "value": True}).error == "invalid_value"
    assert r.execute("d", "s5", {"action": "media.seek"}).error == "invalid_value"
    assert b.calls == [("seek", 95)]
