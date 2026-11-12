"""One-time pairing codes shown in the PC app and typed on the deck.

- 6 random digits (``secrets``), valid ``ttl_s`` seconds, single use.
- At most ``max_attempts`` wrong guesses per code; then the code is burned and a new one is needed.
- Codes exist only while the user has the pairing panel open (``start()`` / ``cancel()``).
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class PairingState:
    active: bool
    code: str | None
    expires_at: float  # time.monotonic() deadline
    attempts_left: int


class PairingManager:
    def __init__(self, ttl_s: float = 120.0, max_attempts: int = 5, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl_s = ttl_s
        self.max_attempts = max_attempts
        self._clock = clock
        self._lock = threading.Lock()
        self._code: str | None = None
        self._expires = 0.0
        self._attempts = 0
        self.listeners: list[Callable[[PairingState], None]] = []

    def _notify(self) -> None:
        st = self.state()
        for cb in list(self.listeners):
            cb(st)

    def start(self) -> str:
        with self._lock:
            self._code = f"{secrets.randbelow(1_000_000):06d}"
            self._expires = self._clock() + self.ttl_s
            self._attempts = self.max_attempts
            code = self._code
        self._notify()
        return code

    def cancel(self) -> None:
        with self._lock:
            self._code = None
        self._notify()

    def state(self) -> PairingState:
        with self._lock:
            active = self._code is not None and self._clock() < self._expires
            return PairingState(active, self._code if active else None, self._expires, self._attempts)

    def consume(self, code: str) -> bool:
        """Check a code typed on the deck. A correct code is consumed (single use)."""
        with self._lock:
            if self._code is None or self._clock() >= self._expires or self._attempts <= 0:
                return False
            candidate = code if isinstance(code, str) else ""
            ok = hmac.compare_digest(self._code.encode(), candidate.strip().encode())
            if ok:
                self._code = None
            else:
                self._attempts -= 1
                if self._attempts <= 0:
                    self._code = None  # burned
        self._notify()
        return ok
