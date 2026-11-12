import asyncio
import json
import sys

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from pcdeck_agent import protocol as P
from pcdeck_agent.hardware.demo_frames import demo_provider
from pcdeck_agent.security.credentials import CredentialStore
from pcdeck_agent.security.pairing import PairingManager
from pcdeck_agent.server import AgentServer

win_only = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")


# ---- DPAPI / credential store ---------------------------------------------------------------------
@win_only
def test_dpapi_roundtrip_and_no_plaintext(tmp_path):
    from pcdeck_agent.security import dpapi

    blob = dpapi.protect(b"secret-payload")
    assert b"secret-payload" not in blob
    assert dpapi.unprotect(blob) == b"secret-payload"


@win_only
def test_store_persists_hash_only_encrypted(tmp_path):
    p = tmp_path / "devices.dat"
    s = CredentialStore(p)
    token = s.add("deck-1", "Escritorio")
    raw = p.read_bytes()
    assert token.encode() not in raw and b"deck-1" not in raw  # encrypted at rest
    s2 = CredentialStore(p)
    assert s2.verify("deck-1", token)
    assert not s2.verify("deck-1", token + "x")
    assert not s2.verify("deck-2", token)
    assert [d.name for d in s2.devices()] == ["Escritorio"]
    assert s2.revoke("deck-1") and not s2.verify("deck-1", token)


def test_store_unreadable_file_starts_empty(tmp_path):
    p = tmp_path / "devices.dat"
    p.write_bytes(b"garbage")
    s = CredentialStore(p, encrypt=sys.platform == "win32")
    assert s.devices() == []
    assert (tmp_path / "devices.unreadable").exists()


def test_repairing_replaces_token(tmp_path):
    s = CredentialStore(tmp_path / "d.dat", encrypt=False)
    t1 = s.add("deck-1", "a")
    t2 = s.add("deck-1", "a")
    assert t1 != t2 and not s.verify("deck-1", t1) and s.verify("deck-1", t2)


# ---- pairing codes ------------------------------------------------------------------------------
def test_pairing_code_single_use_and_expiry():
    t = [0.0]
    pm = PairingManager(ttl_s=120, max_attempts=3, clock=lambda: t[0])
    code = pm.start()
    assert len(code) == 6 and code.isdigit()
    assert pm.consume(code)
    assert not pm.consume(code)  # single use
    code = pm.start()
    t[0] = 121
    assert not pm.consume(code)  # expired
    assert not pm.state().active


def test_pairing_code_burned_after_max_attempts():
    pm = PairingManager(max_attempts=3)
    code = pm.start()
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(3):
        assert not pm.consume(wrong)
    assert not pm.consume(code)  # burned even with the right code
    assert not pm.state().active


def test_no_code_until_started():
    assert not PairingManager().consume("123456")


# ---- end-to-end pairing / auth over WebSocket -----------------------------------------------------
HELLO = json.dumps({"v": 1, "type": "hello", "payload": {"clientId": "deck-test", "fw": "t", "protocol": 1}})


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=20))


async def with_server(tmp_path, body):
    creds = CredentialStore(tmp_path / "devices.dat", encrypt=False)
    pairing = PairingManager()
    srv = AgentServer(demo_provider(), hosts=["127.0.0.1"], port=0, device_id="pc", interval_s=0.5, credentials=creds, pairing=pairing, agent_id="agent-1")
    stop = asyncio.Event()
    task = asyncio.create_task(srv.run(stop))
    await srv.wait_ready()
    try:
        return await body(f"ws://127.0.0.1:{srv.bound[0][1]}", srv, creds, pairing)
    finally:
        stop.set()
        await task


async def next_types(ws, n=6, timeout=1.5):
    out = []
    try:
        for _ in range(n):
            out.append(json.loads(await asyncio.wait_for(ws.recv(), timeout)))
    except asyncio.TimeoutError:
        pass
    return out


def test_unpaired_client_gets_no_telemetry(tmp_path):
    async def body(url, srv, creds, pairing):
        async with connect(url) as ws:
            await ws.send(HELLO)
            caps = json.loads(await ws.recv())
            assert caps["type"] == P.T_CAPABILITIES
            assert caps["payload"]["authRequired"] is True and caps["payload"]["authenticated"] is False
            assert caps["payload"]["notes"] == [] and caps["payload"]["provider"] == ""
            msgs = await next_types(ws)
            assert all(m["type"] != P.T_SNAPSHOT for m in msgs)

    run(with_server(tmp_path, body))


def test_pair_then_auth_then_snapshots(tmp_path):
    async def body(url, srv, creds, pairing):
        code = pairing.start()
        async with connect(url) as ws:
            await ws.send(HELLO)
            await ws.recv()  # caps (unauthenticated)
            await ws.send(json.dumps({"v": 1, "type": "pair.request", "payload": {"code": code, "name": "Mesa"}}))
            res = json.loads(await ws.recv())
            assert res["type"] == P.T_PAIR_RESULT and res["payload"]["ok"] is True
            token = res["payload"]["token"]
            assert res["payload"]["agentId"] == "agent-1"
            types = [m["type"] for m in await next_types(ws)]
            assert P.T_CAPABILITIES in types and P.T_SNAPSHOT in types
        # reconnect with the token
        async with connect(url) as ws:
            await ws.send(HELLO)
            await ws.recv()
            await ws.send(json.dumps({"v": 1, "type": "auth.request", "payload": {"token": token}}))
            res = json.loads(await ws.recv())
            assert res["type"] == P.T_AUTH_RESULT and res["payload"]["ok"] is True
            types = [m["type"] for m in await next_types(ws)]
            assert P.T_SNAPSHOT in types
        assert [d.name for d in creds.devices()] == ["Mesa"]

    run(with_server(tmp_path, body))


def test_wrong_code_and_bad_token_rejected_then_closed(tmp_path):
    async def body(url, srv, creds, pairing):
        code = pairing.start()
        wrong = "000000" if code != "000000" else "111111"
        async with connect(url) as ws:
            await ws.send(HELLO)
            await ws.recv()
            await ws.send(json.dumps({"v": 1, "type": "pair.request", "payload": {"code": wrong}}))
            res = json.loads(await ws.recv())
            assert res["payload"] == {"ok": False, "error": P.E_INVALID_CODE}
            with pytest.raises(ConnectionClosed):
                for _ in range(10):
                    await ws.send(json.dumps({"v": 1, "type": "auth.request", "payload": {"token": "x" * 43}}))
                    m = json.loads(await ws.recv())
                    assert m["type"] in (P.T_AUTH_RESULT, P.T_HEARTBEAT)
                    if m["type"] == P.T_AUTH_RESULT:
                        assert m["payload"]["ok"] is False
        assert creds.devices() == []

    run(with_server(tmp_path, body))


def test_token_of_other_device_rejected(tmp_path):
    async def body(url, srv, creds, pairing):
        token = creds.add("someone-else", "x")
        async with connect(url) as ws:
            await ws.send(HELLO)  # clientId deck-test
            await ws.recv()
            await ws.send(json.dumps({"v": 1, "type": "auth.request", "payload": {"token": token}}))
            res = json.loads(await ws.recv())
            assert res["payload"]["ok"] is False

    run(with_server(tmp_path, body))


def test_commands_unauthorized_before_auth(tmp_path):
    async def body(url, srv, creds, pairing):
        async with connect(url) as ws:
            await ws.send(HELLO)
            await ws.recv()
            await ws.send(json.dumps({"v": 1, "type": "command.request", "requestId": "c1", "payload": {"action": "session.lock"}}))
            res = json.loads(await ws.recv())
            assert res["type"] == P.T_COMMAND_RESULT and res["payload"]["error"] == P.E_UNAUTHORIZED

    run(with_server(tmp_path, body))


@pytest.mark.parametrize(
    "payload",
    [{"code": "12345"}, {"code": "abcdef"}, {"code": 123456}, {"code": "123456", "name": "x" * 41}],
)
def test_pair_request_validation(payload):
    with pytest.raises(P.ProtocolError):
        P.decode(json.dumps({"v": 1, "type": "pair.request", "payload": payload}))
