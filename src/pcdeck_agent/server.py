"""WebSocket server: authenticated telemetry for paired decks.

- Sensor sampling runs in a worker thread (``asyncio.to_thread``) so slow hardware never blocks I/O.
- Only private / loopback / link-local peers are accepted; max N concurrent clients.
- A client must send ``hello`` within HELLO_TIMEOUT_S.
- With a CredentialStore (production), nothing but capabilities is sent until the client either
  authenticates (``auth.request`` with its device token) or pairs (``pair.request`` with the
  one-time code shown in the PC app). Failed attempts are rate-limited per peer.
- ``command.request`` is answered ``not_supported`` (authenticated) or ``unauthorized``; no
  command is executed yet.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from . import __version__
from . import protocol as P
from .hardware.mapper import MapperConfig, SensorMapper
from .hardware.provider import HardwareMetricsProvider
from .commands.router import CommandRouter
from .security.credentials import CredentialStore
from .security.pairing import PairingManager

log = logging.getLogger(__name__)

HELLO_TIMEOUT_S = 5.0
HEARTBEAT_S = 5.0
RATE_LIMIT_PER_S = 20
MAX_AUTH_FAILURES_PER_SESSION = 5
MAX_AUTH_FAILURES_PER_PEER_MIN = 10


def peer_allowed(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local


@dataclass(eq=False)  # identity hashing: sessions live in a set
class ClientSession:
    ws: ServerConnection
    peer: str
    client_id: str | None = None
    fw: str | None = None
    name: str = ""
    authenticated: bool = False
    auth_failures: int = 0
    sent_art_id: int = 0
    connected_at: float = field(default_factory=time.time)
    recent: deque = field(default_factory=lambda: deque(maxlen=RATE_LIMIT_PER_S + 1))

    def rate_exceeded(self) -> bool:
        now = time.monotonic()
        self.recent.append(now)
        return len(self.recent) > RATE_LIMIT_PER_S and now - self.recent[0] < 1.0


@dataclass(frozen=True)
class SessionInfo:
    client_id: str | None
    name: str
    peer: str
    authenticated: bool
    connected_at: float
    fw: str | None = None


class AgentServer:
    def __init__(
        self,
        provider: HardwareMetricsProvider,
        *,
        hosts: list[str],
        port: int,
        device_id: str,
        interval_s: float = 1.0,
        max_clients: int = 4,
        mapper_config: MapperConfig | None = None,
        hello_timeout_s: float = HELLO_TIMEOUT_S,
        credentials: CredentialStore | None = None,
        pairing: PairingManager | None = None,
        agent_id: str = "",
        commands: CommandRouter | None = None,
        battery: Any = None,
    ) -> None:
        self.commands = commands
        self.battery = battery  # hardware.battery.BatteryReader (None: not reported)
        self.provider = provider
        self.hosts = hosts
        self.port = port
        self.device_id = device_id
        self.agent_id = agent_id or device_id
        self.interval_s = interval_s
        self.max_clients = max_clients
        self.hello_timeout_s = hello_timeout_s
        self.credentials = credentials
        self.pairing = pairing
        self.require_auth = credentials is not None
        self.mapper = SensorMapper(mapper_config or MapperConfig(host=device_id))
        self.sessions: set[ClientSession] = set()
        self.latest_snapshot: str | None = None
        self.latest_art = None  # commands.artwork.Artwork | None
        self.samples = 0
        self.last_sample_ms = 0.0
        self.provider_notes: tuple[str, ...] = ()
        self._ready = asyncio.Event()
        self._peer_failures: dict[str, deque] = {}
        self.bound: list[tuple[str, int]] = []
        # Hooks for the tray UI (called from the asyncio thread).
        self.on_sessions_changed: Callable[[list[SessionInfo]], None] | None = None
        self.on_paired: Callable[[str, str], None] | None = None

    # ---- lifecycle ------------------------------------------------------------------------------
    async def run(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        await asyncio.to_thread(self.provider.open)
        self.provider_notes = self.provider.status().notes
        try:
            async with serve(self._handler, self.hosts, self.port, max_size=P.MAX_MESSAGE_BYTES, ping_interval=20, ping_timeout=20) as server:
                self.bound = [s.getsockname()[:2] for s in server.sockets]
                for addr in self.bound:
                    log.info("listening on ws://%s:%d (LAN only; plain ws:// - telemetry unencrypted)", *addr)
                self._ready.set()
                sampler = asyncio.create_task(self._sampler(stop))
                await stop.wait()
                sampler.cancel()
        finally:
            await asyncio.to_thread(self.provider.close)

    async def wait_ready(self) -> None:
        await self._ready.wait()

    def session_infos(self) -> list[SessionInfo]:
        return [SessionInfo(s.client_id, s.name, s.peer, s.authenticated, s.connected_at, (s.fw or "")[:24] or None) for s in self.sessions]

    def _sessions_changed(self) -> None:
        if self.on_sessions_changed:
            try:
                self.on_sessions_changed(self.session_infos())
            except Exception:
                log.exception("on_sessions_changed hook failed")

    async def push_capabilities(self) -> None:
        """Re-send capabilities to authenticated sessions (e.g. power actions enabled/disabled)."""
        for s in list(self.sessions):
            if s.authenticated:
                try:
                    await self._send(s.ws, self._capabilities(s))
                except ConnectionClosed:
                    pass

    async def disconnect_client(self, client_id: str) -> None:
        """Close the connection(s) of a device, e.g. after revoking it."""
        for s in list(self.sessions):
            if s.client_id == client_id:
                await s.ws.close(1008, "revoked")

    # ---- sampling + broadcast ---------------------------------------------------------------------
    def _sample_blocking(self) -> str:
        t0 = time.perf_counter()
        status = self.provider.status()
        self.provider_notes = status.notes
        snap = self.mapper.map(self.provider.read(), self.provider.os_memory(), status.notes)
        media = None
        if self.commands is not None and self.commands.backend is not None:
            try:
                media = self.commands.backend.state()  # volume + now playing + artwork
            except Exception:
                log.debug("media state unavailable", exc_info=True)
        self.latest_art = getattr(media, "art", None)
        battery = self.battery.read() if self.battery is not None else None
        msg = P.encode(P.envelope(P.T_SNAPSHOT, P.snapshot_payload(snap, media, battery), device_id=self.device_id))
        self.last_sample_ms = (time.perf_counter() - t0) * 1000
        return msg

    async def _sampler(self, stop: asyncio.Event) -> None:
        await asyncio.to_thread(self.provider.read)  # prime load sensors
        next_t = time.monotonic()
        while not stop.is_set():
            next_t += self.interval_s
            try:
                self.latest_snapshot = await asyncio.to_thread(self._sample_blocking)
                self.samples += 1
            except Exception:  # keep serving even if one sample fails
                log.exception("sampling failed")
            await self._broadcast()
            await asyncio.sleep(max(0.0, next_t - time.monotonic()))

    async def _broadcast(self) -> None:
        if not self.latest_snapshot:
            return
        for s in list(self.sessions):
            if s.client_id is None or (self.require_auth and not s.authenticated):
                continue
            try:
                await s.ws.send(self.latest_snapshot)
                await self._send_art(s)
            except ConnectionClosed:
                pass

    async def _send_art(self, s: ClientSession) -> None:
        """Push the current album art once per track (binary frames, see commands/artwork.py)."""
        art = self.latest_art
        if art is None or s.sent_art_id == art.art_id:
            return
        from .commands.artwork import chunks

        for frame in chunks(art):
            await s.ws.send(frame)
        s.sent_art_id = art.art_id

    # ---- per-connection ---------------------------------------------------------------------------
    def _capabilities(self, sess: ClientSession) -> dict[str, Any]:
        granted = not self.require_auth or sess.authenticated
        return P.envelope(
            P.T_CAPABILITIES,
            {
                "agent": f"pcdeck-agent {__version__}",
                "agentId": self.agent_id,
                "agentName": self.device_id,
                "provider": self.provider.status().name if granted else "",
                "actions": self.commands.available_actions() if (granted and self.commands) else [],
                "notes": list(self.provider_notes) if granted else [],
                "authRequired": self.require_auth,
                "authenticated": granted,
            },
            device_id=self.device_id,
        )

    async def _send(self, ws: ServerConnection, msg: dict[str, Any]) -> None:
        await ws.send(P.encode(msg))

    def _peer_blocked(self, peer: str) -> bool:
        q = self._peer_failures.get(peer)
        now = time.monotonic()
        if not q:
            return False
        while q and now - q[0] > 60:
            q.popleft()
        return len(q) >= MAX_AUTH_FAILURES_PER_PEER_MIN

    def _record_failure(self, sess: ClientSession) -> None:
        sess.auth_failures += 1
        self._peer_failures.setdefault(sess.peer, deque(maxlen=64)).append(time.monotonic())

    async def _handler(self, ws: ServerConnection) -> None:
        peer = ws.remote_address[0] if ws.remote_address else "?"
        if not peer_allowed(peer):
            log.warning("rejected non-LAN peer %s", peer)
            await ws.close(1008, "LAN clients only")
            return
        if self._peer_blocked(peer):
            log.warning("rejected %s: too many failed auth/pair attempts in the last minute", peer)
            await ws.close(1008, "too many failed attempts")
            return
        if len(self.sessions) >= self.max_clients:
            log.warning("rejected %s: max clients (%d)", peer, self.max_clients)
            await ws.close(1013, "too many clients")
            return
        sess = ClientSession(ws=ws, peer=peer)
        self.sessions.add(sess)
        log.info("client connected from %s", peer)
        hb = asyncio.create_task(self._heartbeat(ws))
        try:
            await self._expect_hello(sess)
            self._sessions_changed()
            await self._send(ws, self._capabilities(sess))
            if not self.require_auth and self.latest_snapshot:
                await ws.send(self.latest_snapshot)
            async for frame in ws:
                if sess.rate_exceeded():
                    log.warning("client %s exceeded %d msg/s, closing", peer, RATE_LIMIT_PER_S)
                    await ws.close(1008, "rate limited")
                    return
                await self._on_message(sess, frame)
        except asyncio.TimeoutError:
            log.warning("client %s sent no hello within %.0f s", peer, self.hello_timeout_s)
            await self._safe_error_close(ws, P.E_EXPECTED_HELLO, "hello required")
        except P.ProtocolError as e:
            log.warning("protocol error from %s: %s (%s)", peer, e.code, e.message)
            await self._safe_error_close(ws, e.code, e.message)
        except ConnectionClosed:
            pass
        finally:
            hb.cancel()
            self.sessions.discard(sess)
            log.info("client %s (%s) disconnected", sess.client_id or "?", peer)
            if sess.authenticated and self.credentials is not None:
                try:
                    self.credentials.touch_save()  # persist "last seen" for the PC app
                except OSError:
                    log.exception("could not persist last_seen")
            self._sessions_changed()

    async def _expect_hello(self, sess: ClientSession) -> None:
        frame = await asyncio.wait_for(sess.ws.recv(), timeout=self.hello_timeout_s)
        msg = P.decode(frame)
        if msg["type"] != P.T_HELLO:
            raise P.ProtocolError(P.E_EXPECTED_HELLO, f"first message must be hello, got {msg['type']}")
        sess.client_id = msg["payload"]["clientId"]
        fw = msg["payload"].get("fw")
        sess.fw = fw if isinstance(fw, str) else None
        log.info("hello from %s client=%s fw=%s", sess.peer, sess.client_id, sess.fw)

    async def _granted(self, sess: ClientSession) -> None:
        """Client just authenticated or paired: send full capabilities and the latest snapshot."""
        sess.authenticated = True
        self._sessions_changed()
        await self._send(sess.ws, self._capabilities(sess))
        if self.latest_snapshot:
            await sess.ws.send(self.latest_snapshot)

    async def _auth_failed(self, sess: ClientSession, reply_type: str, code: str) -> None:
        self._record_failure(sess)
        await self._send(sess.ws, P.envelope(reply_type, {"ok": False, "error": code}, device_id=self.device_id))
        if sess.auth_failures >= MAX_AUTH_FAILURES_PER_SESSION:
            log.warning("client %s (%s): too many failed attempts, closing", sess.client_id, sess.peer)
            await sess.ws.close(1008, "too many failed attempts")

    async def _on_pair(self, sess: ClientSession, payload: dict[str, Any]) -> None:
        if not self.require_auth or self.credentials is None or self.pairing is None:
            await self._send(sess.ws, P.envelope(P.T_PAIR_RESULT, {"ok": False, "error": P.E_NOT_SUPPORTED}, device_id=self.device_id))
            return
        if not self.pairing.consume(payload["code"]):
            log.warning("pairing attempt from %s (%s) with a wrong or expired code", sess.client_id, sess.peer)
            await self._auth_failed(sess, P.T_PAIR_RESULT, P.E_INVALID_CODE)
            return
        name = payload.get("name") or sess.client_id or "deck"
        sess.name = name
        assert sess.client_id is not None
        token = await asyncio.to_thread(self.credentials.add, sess.client_id, name)
        await self._send(
            sess.ws,
            P.envelope(P.T_PAIR_RESULT, {"ok": True, "token": token, "agentId": self.agent_id, "agentName": self.device_id}, device_id=self.device_id),
        )
        if self.on_paired:
            self.on_paired(sess.client_id, name)
        await self._granted(sess)

    async def _on_auth(self, sess: ClientSession, payload: dict[str, Any]) -> None:
        if not self.require_auth or self.credentials is None:
            await self._send(sess.ws, P.envelope(P.T_AUTH_RESULT, {"ok": True}, device_id=self.device_id))
            return
        assert sess.client_id is not None
        if not self.credentials.verify(sess.client_id, payload["token"]):
            log.warning("authentication failed for %s (%s)", sess.client_id, sess.peer)
            await self._auth_failed(sess, P.T_AUTH_RESULT, P.E_UNAUTHORIZED)
            return
        dev = next((d for d in self.credentials.devices() if d.client_id == sess.client_id), None)
        sess.name = dev.name if dev else sess.client_id
        await self._send(sess.ws, P.envelope(P.T_AUTH_RESULT, {"ok": True}, device_id=self.device_id))
        log.info("client %s authenticated", sess.client_id)
        await self._granted(sess)

    async def _on_message(self, sess: ClientSession, frame: str | bytes) -> None:
        try:
            msg = P.decode(frame)
        except P.ProtocolError as e:
            if e.code == P.E_UNSUPPORTED_VERSION:
                raise
            log.info("rejected message from %s: %s (%s)", sess.peer, e.code, e.message)
            await self._send(sess.ws, P.error_msg(e.code, e.message, self.device_id))
            return
        t = msg["type"]
        if t in (P.T_HEARTBEAT, P.T_HELLO):
            return
        if t == P.T_PAIR_REQUEST:
            await self._on_pair(sess, msg["payload"])
            return
        if t == P.T_AUTH_REQUEST:
            await self._on_auth(sess, msg["payload"])
            return
        if t == P.T_COMMAND_REQUEST:
            granted = not self.require_auth or sess.authenticated
            rid = msg.get("requestId")
            if granted and self.commands is not None and isinstance(rid, str) and 1 <= len(rid) <= 40:
                outcome = await asyncio.to_thread(self.commands.execute, sess.client_id or sess.peer, rid, msg["payload"])
                await self._send(sess.ws, P.envelope(P.T_COMMAND_RESULT, outcome.payload(), device_id=self.device_id, request_id=rid))
                return
            if isinstance(msg.get("requestId"), str):
                action = msg["payload"].get("action") if isinstance(msg["payload"].get("action"), str) else "?"
                await self._send(
                    sess.ws,
                    P.envelope(
                        P.T_COMMAND_RESULT,
                        {"ok": False, "action": action[:32], "error": P.E_NOT_SUPPORTED if granted else P.E_UNAUTHORIZED},
                        device_id=self.device_id,
                        request_id=msg["requestId"][:40],
                    ),
                )
            log.info("rejected command from %s: %s", sess.peer, "not supported yet" if granted else "unauthorized")
            return
        await self._send(sess.ws, P.error_msg(P.E_UNKNOWN_TYPE, f"{t} not accepted from clients", self.device_id))

    async def _heartbeat(self, ws: ServerConnection) -> None:
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_S)
                await self._send(ws, P.envelope(P.T_HEARTBEAT, {}, device_id=self.device_id))
        except (ConnectionClosed, asyncio.CancelledError):
            pass

    async def _safe_error_close(self, ws: ServerConnection, code: str, message: str) -> None:
        try:
            await self._send(ws, P.error_msg(code, message, self.device_id))
            await ws.close(1002 if code in (P.E_MALFORMED, P.E_UNSUPPORTED_VERSION) else 1008, code)
        except ConnectionClosed:
            pass
