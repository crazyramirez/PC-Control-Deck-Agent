"""Agent runtime: WebSocket server + credentials + pairing + mDNS, running on a background thread.

Used by both the CLI (``serve``) and the tray app. All UI-facing state changes are delivered through
plain callbacks invoked from the runtime thread; the Qt layer re-emits them as signals.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from .commands.router import CommandRouter, default_backend
from .config import AgentConfig, app_dir, resolve_bind
from .discovery import Advertiser
from .hardware.mapper import MapperConfig
from .hardware.provider import HardwareMetricsProvider
from .platforms import battery_reader
from .security.credentials import CredentialStore, PairedDevice
from .security.pairing import PairingManager, PairingState
from .server import AgentServer, SessionInfo

log = logging.getLogger(__name__)


@dataclass
class RuntimeStatus:
    running: bool = False
    error: str = ""
    endpoints: list[str] = field(default_factory=list)  # "192.168.1.10:8765"
    mdns: bool = False
    notes: tuple[str, ...] = ()
    provider: str = ""


class AgentRuntime:
    def __init__(self, cfg: AgentConfig, provider_factory: Callable[[], HardwareMetricsProvider]) -> None:
        self.cfg = cfg
        self._provider_factory = provider_factory
        self.enable_commands = True  # volume / media / lock on the local desktop session
        self.credentials = CredentialStore(app_dir() / "devices.dat")
        self.pairing = PairingManager()
        self.status = RuntimeStatus()
        self.server: AgentServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._thread: threading.Thread | None = None
        self._advertiser: Advertiser | None = None
        # Callbacks (runtime thread)
        self.on_status: Callable[[RuntimeStatus], None] | None = None
        self.on_sessions: Callable[[list[SessionInfo]], None] | None = None
        self.on_paired: Callable[[str, str], None] | None = None
        self.on_pairing: Callable[[PairingState], None] | None = None
        self.pairing.listeners.append(lambda st: self.on_pairing and self.on_pairing(st))

    # ---- lifecycle ------------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_thread, name="pcdeck-agent", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        if self._loop and self._stop:
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread:
            self._thread.join(timeout)
        self._thread = None

    def _emit_status(self) -> None:
        if self.on_status:
            try:
                self.on_status(self.status)
            except Exception:
                log.exception("on_status callback failed")

    def _run_thread(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as e:  # e.g. port in use, LHM missing
            log.exception("agent runtime stopped with an error")
            self.status.running = False
            self.status.error = f"{type(e).__name__}: {e}"
            self._emit_status()

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        hosts = resolve_bind(self.cfg.bind)
        provider = self._provider_factory()
        self.server = AgentServer(
            provider,
            hosts=hosts,
            port=self.cfg.port,
            device_id=self.cfg.device_id,
            interval_s=self.cfg.interval_s,
            max_clients=self.cfg.max_clients,
            mapper_config=MapperConfig(
                host=self.cfg.device_id, cpu_temp_sensor_id=self.cfg.cpu_temp_sensor_id, gpu_hardware_id=self.cfg.gpu_hardware_id
            ),
            credentials=self.credentials,
            pairing=self.pairing,
            agent_id=self.cfg.agent_id,
            commands=CommandRouter(
                await asyncio.to_thread(default_backend, lambda: self.cfg.allow_power) if self.enable_commands else None
            ),
            battery=battery_reader() if self.enable_commands else None,  # real hardware only (not --fake)
        )
        self.server.on_sessions_changed = lambda s: self.on_sessions and self.on_sessions(s)
        self.server.on_paired = lambda cid, name: self.on_paired and self.on_paired(cid, name)
        run_task = asyncio.create_task(self.server.run(self._stop))
        ready = asyncio.create_task(self.server.wait_ready())
        done, _ = await asyncio.wait({run_task, ready}, return_when=asyncio.FIRST_COMPLETED)
        if run_task in done:
            run_task.result()  # raises the startup error (e.g. port in use)
            return
        lan = [h for h, _ in self.server.bound if not h.startswith("127.")]
        self.status = RuntimeStatus(
            running=True,
            endpoints=[f"{h}:{p}" for h, p in self.server.bound if not h.startswith("127.")],
            notes=self.server.provider_notes,
            provider=provider.status().name,
        )
        if self.cfg.mdns:
            self._advertiser = Advertiser(agent_id=self.cfg.agent_id, name=self.cfg.device_id, port=self.cfg.port, addresses=lan)
            self.status.mdns = await asyncio.to_thread(self._advertiser.start)
        self._emit_status()
        try:
            await run_task
        finally:
            if self._advertiser:
                await asyncio.to_thread(self._advertiser.stop)
            self.status.running = False
            self._emit_status()

    # ---- UI actions (any thread) ------------------------------------------------------------------
    def start_pairing(self) -> str:
        return self.pairing.start()

    def cancel_pairing(self) -> None:
        self.pairing.cancel()

    def devices(self) -> list[PairedDevice]:
        return self.credentials.devices()

    def sessions(self) -> list[SessionInfo]:
        return self.server.session_infos() if self.server else []

    def set_allow_power(self, on: bool) -> None:
        from .config import save_config

        self.cfg.allow_power = on
        save_config(self.cfg)
        log.info("power actions from devices %s", "ENABLED" if on else "disabled")
        if self._loop and self.server:
            asyncio.run_coroutine_threadsafe(self.server.push_capabilities(), self._loop)

    def revoke(self, client_id: str) -> None:
        self.credentials.revoke(client_id)
        if self._loop and self.server:
            asyncio.run_coroutine_threadsafe(self.server.disconnect_client(client_id), self._loop)
