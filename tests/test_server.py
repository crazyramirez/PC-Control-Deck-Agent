"""End-to-end tests of the WebSocket server on loopback with the fake provider."""

import asyncio
import json

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from pcdeck_agent import protocol as P
from pcdeck_agent.hardware.demo_frames import demo_provider
from pcdeck_agent.server import AgentServer, peer_allowed

HELLO = json.dumps({"v": 1, "type": "hello", "payload": {"clientId": "test-deck", "fw": "t", "protocol": 1}})


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=20))


async def with_server(body, **kw):
    srv = AgentServer(demo_provider(), hosts=["127.0.0.1"], port=0, device_id="test-agent", interval_s=0.5, **kw)
    stop = asyncio.Event()
    task = asyncio.create_task(srv.run(stop))
    await srv.wait_ready()
    try:
        port = srv.bound[0][1]
        return await body(f"ws://127.0.0.1:{port}", srv)
    finally:
        stop.set()
        await task


async def recv_type(ws, msg_type, limit=10):
    for _ in range(limit):
        m = json.loads(await ws.recv())
        if m["type"] == msg_type:
            return m
    raise AssertionError(f"no {msg_type} received")


def test_hello_then_capabilities_then_snapshots():
    async def body(url, srv):
        async with connect(url) as ws:
            await ws.send(HELLO)
            caps = json.loads(await ws.recv())
            assert caps["type"] == P.T_CAPABILITIES
            assert caps["payload"]["actions"] == [] and caps["payload"]["authRequired"] is False
            s1 = await recv_type(ws, P.T_SNAPSHOT)
            s2 = await recv_type(ws, P.T_SNAPSHOT)
            assert s1["deviceId"] == "test-agent"
            assert s1["payload"]["cpu"]["name"] == "Fake CPU (synthetic)"
            assert s1["payload"]["ram"]["totalMiB"] == 65536
            assert s1 != s2

    run(with_server(body))


def test_no_hello_closes_connection():
    async def body(url, srv):
        async with connect(url) as ws:
            err = json.loads(await ws.recv())
            assert err["type"] == P.T_ERROR and err["payload"]["code"] == P.E_EXPECTED_HELLO
            with pytest.raises(ConnectionClosed):
                await ws.recv()

    run(with_server(body, hello_timeout_s=0.3))


def test_wrong_first_message_is_rejected():
    async def body(url, srv):
        async with connect(url) as ws:
            await ws.send(json.dumps({"v": 1, "type": "heartbeat", "payload": {}}))
            err = json.loads(await ws.recv())
            assert err["payload"]["code"] == P.E_EXPECTED_HELLO

    run(with_server(body))


def test_unsupported_version_closes():
    async def body(url, srv):
        async with connect(url) as ws:
            await ws.send(json.dumps({"v": 2, "type": "hello", "payload": {"clientId": "x"}}))
            err = json.loads(await ws.recv())
            assert err["payload"]["code"] == P.E_UNSUPPORTED_VERSION
            with pytest.raises(ConnectionClosed):
                await ws.recv()

    run(with_server(body))


def test_command_rejected_unauthorized_with_correlation():
    async def body(url, srv):
        async with connect(url) as ws:
            await ws.send(HELLO)
            await ws.send(json.dumps({"v": 1, "type": "command.request", "requestId": "cmd-7", "payload": {"action": "volume.set", "value": 10}}))
            res = await recv_type(ws, P.T_COMMAND_RESULT)
            assert res["requestId"] == "cmd-7"
            assert res["payload"] == {"ok": False, "action": "volume.set", "error": P.E_NOT_SUPPORTED}  # open mode, nothing executed yet

    run(with_server(body))


def test_malformed_after_hello_gets_error_but_stays_open():
    async def body(url, srv):
        async with connect(url) as ws:
            await ws.send(HELLO)
            await ws.send("{not json")
            err = await recv_type(ws, P.T_ERROR)
            assert err["payload"]["code"] == P.E_MALFORMED
            await recv_type(ws, P.T_SNAPSHOT)  # still streaming

    run(with_server(body))


def test_oversized_frame_closes():
    async def body(url, srv):
        async with connect(url, max_size=None) as ws:
            await ws.send(HELLO)
            await ws.send("x" * (P.MAX_MESSAGE_BYTES + 10))
            with pytest.raises(ConnectionClosed):
                for _ in range(10):
                    await ws.recv()

    run(with_server(body))


def test_rate_limit_closes():
    async def body(url, srv):
        async with connect(url) as ws:
            await ws.send(HELLO)
            hb = json.dumps({"v": 1, "type": "heartbeat", "payload": {}})
            with pytest.raises(ConnectionClosed):
                for _ in range(100):
                    await ws.send(hb)
                for _ in range(20):
                    await ws.recv()

    run(with_server(body))


def test_max_clients():
    async def body(url, srv):
        async with connect(url) as a:
            await a.send(HELLO)
            await a.recv()
            async with connect(url) as b:
                with pytest.raises(ConnectionClosed) as e:
                    await b.recv()
                assert e.value.rcvd.code == 1013

    run(with_server(body, max_clients=1))


@pytest.mark.parametrize(
    "ip,ok",
    [
        ("192.168.1.20", True),
        ("10.0.0.5", True),
        ("172.16.3.4", True),
        ("127.0.0.1", True),
        ("169.254.10.1", True),
        ("::1", True),
        ("::ffff:192.168.1.7", True),
        ("8.8.8.8", False),
        ("2001:4860:4860::8888", False),
        ("not-an-ip", False),
    ],
)
def test_peer_allowed(ip, ok):
    assert peer_allowed(ip) is ok
