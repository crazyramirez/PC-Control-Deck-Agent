"""Allow-listed command execution (spec §4 "Acciones remotas V1").

Only the actions below exist; there is no generic execution path. Every request is validated
(type, range), de-duplicated by requestId and rate-limited per device before it reaches Windows.
Shutdown/restart exist only when the PC owner enables them in the PC app (off by default, spec §4);
the deck also asks for confirmation. Every OS gets a 10 s delay with a visible notice.
Backends: WindowsBackend (below), commands/linux_media.LinuxBackend, commands/macos_media.MacBackend.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger(__name__)

ACTIONS = (
    "volume.set", "media.play_pause", "media.next", "media.previous", "media.seek",
    "session.lock", "system.shutdown", "system.restart",
)
MAX_SEEK_S = 86400
POWER_ACTIONS = ("system.shutdown", "system.restart")
POWER_DELAY_S = 10
MAX_COMMANDS_PER_SECOND = 8
DEDUP_WINDOW = 64


@dataclass
class CommandOutcome:
    ok: bool
    action: str
    applied_value: int | None = None
    error: str | None = None

    def payload(self) -> dict[str, Any]:
        p: dict[str, Any] = {"ok": self.ok, "action": self.action}
        if self.applied_value is not None:
            p["appliedValue"] = self.applied_value
        if self.error:
            p["error"] = self.error

        return p


class Backend(Protocol):
    def set_volume(self, pct: int) -> int: ...
    def media(self, op: str) -> bool: ...
    def seek(self, pos_s: int) -> bool: ...
    def lock(self) -> bool: ...
    def power(self, op: str) -> bool: ...
    def state(self) -> Any: ...
    def available(self) -> tuple[str, ...]: ...


class WindowsBackend:
    """Adapter running WindowsMedia coroutines on the media thread (blocking calls)."""

    def __init__(self, allow_power: Any = lambda: False) -> None:
        from .windows_media import WindowsMedia

        self.allow_power = allow_power
        self.media_backend = WindowsMedia()
        self.media_backend.start()

    def power(self, op: str) -> bool:
        import subprocess

        flag = {"shutdown": "/s", "restart": "/r"}[op]
        msg = "PC Control Deck: " + ("apagado solicitado desde el dispositivo" if op == "shutdown" else "reinicio solicitado desde el dispositivo")
        r = subprocess.run(["shutdown", flag, "/t", str(POWER_DELAY_S), "/c", msg], capture_output=True, creationflags=0x08000000, timeout=10)
        return r.returncode == 0

    def set_volume(self, pct: int) -> int:
        return self.media_backend.call(lambda: self.media_backend.a_set_volume(pct))

    def media(self, op: str) -> bool:
        return self.media_backend.call(lambda: self.media_backend.a_media(op))

    def seek(self, pos_s: int) -> bool:
        return self.media_backend.call(lambda: self.media_backend.a_seek(pos_s))

    def lock(self) -> bool:
        from .windows_media import lock_workstation

        return lock_workstation()

    def state(self) -> Any:
        return self.media_backend.call(self.media_backend.a_state, timeout=2.0)

    def available(self) -> tuple[str, ...]:
        acts = ["media.play_pause", "media.next", "media.previous", "media.seek", "session.lock"]  # media keys always work
        if self.media_backend.audio_ok:
            acts.insert(0, "volume.set")
        if self.allow_power():
            acts += list(POWER_ACTIONS)
        return tuple(acts)


class CommandRouter:
    def __init__(self, backend: Backend | None) -> None:
        self.backend = backend
        self._seen: dict[str, OrderedDict[str, CommandOutcome]] = {}
        self._rate: dict[str, deque] = {}

    def available_actions(self) -> list[str]:
        return list(self.backend.available()) if self.backend else []

    def execute(self, client_id: str, request_id: str, payload: dict[str, Any]) -> CommandOutcome:
        """Blocking; call from a worker thread."""
        action = payload.get("action")
        if not isinstance(action, str) or action not in ACTIONS:
            return CommandOutcome(False, str(action)[:32] if action else "?", error="unknown_action")
        if self.backend is None or action not in self.available_actions():
            return CommandOutcome(False, action, error="not_supported")

        # Duplicate / replayed requestId from the same device: return the previous outcome, do not re-run.
        seen = self._seen.setdefault(client_id, OrderedDict())
        if request_id in seen:
            log.info("duplicate command %s from %s ignored", request_id, client_id)
            return seen[request_id]

        q = self._rate.setdefault(client_id, deque(maxlen=MAX_COMMANDS_PER_SECOND + 1))
        now = time.monotonic()
        q.append(now)
        if len(q) > MAX_COMMANDS_PER_SECOND and now - q[0] < 1.0:
            return CommandOutcome(False, action, error="rate_limited")

        outcome = self._run(action, payload)
        seen[request_id] = outcome
        while len(seen) > DEDUP_WINDOW:
            seen.popitem(last=False)
        log.info("command %s %s from %s -> %s", request_id, action, client_id, "ok" if outcome.ok else outcome.error)
        return outcome

    def _run(self, action: str, payload: dict[str, Any]) -> CommandOutcome:
        assert self.backend is not None
        try:
            if action == "volume.set":
                v = payload.get("value")
                if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 100:
                    return CommandOutcome(False, action, error="invalid_value")
                return CommandOutcome(True, action, applied_value=self.backend.set_volume(v))
            if action == "media.seek":
                v = payload.get("value")
                if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= MAX_SEEK_S:
                    return CommandOutcome(False, action, error="invalid_value")
                ok = self.backend.seek(v)
                return CommandOutcome(ok, action, applied_value=v if ok else None, error=None if ok else "failed")
            if action.startswith("media."):
                ok = self.backend.media(action.split(".", 1)[1])
                return CommandOutcome(ok, action, error=None if ok else "failed")
            if action == "session.lock":
                ok = self.backend.lock()
                return CommandOutcome(ok, action, error=None if ok else "failed")
            if action in POWER_ACTIONS:
                ok = self.backend.power(action.split(".", 1)[1])
                return CommandOutcome(ok, action, error=None if ok else "failed")
        except Exception as e:
            log.warning("command %s failed: %s", action, e)
            return CommandOutcome(False, action, error="failed")
        return CommandOutcome(False, action, error="unknown_action")


def default_backend(allow_power: Any = lambda: False) -> Backend | None:
    """Windows: WindowsBackend (unchanged); Linux: MPRIS + PipeWire/PulseAudio; macOS: AppleScript."""
    from ..platforms import command_backend

    try:
        return command_backend(allow_power)
    except Exception as e:
        log.warning("command backend unavailable: %s", e)
        return None
