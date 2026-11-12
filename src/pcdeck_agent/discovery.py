"""mDNS / Bonjour advertisement so decks find the PC without typing an IP.

Service: ``_pcdeck._tcp.local.`` on the agent port, TXT records:
  id=<agent id>  name=<PC name>  v=<protocol major>
The advertisement only says "a PC Control Deck agent lives here"; it carries no secret. Being
discoverable does not grant access: a deck must still pair with the one-time code.
"""

from __future__ import annotations

import logging
import socket

from zeroconf import IPVersion, ServiceInfo, Zeroconf

from . import protocol as P

log = logging.getLogger(__name__)
SERVICE_TYPE = "_pcdeck._tcp.local."


class Advertiser:
    def __init__(self, *, agent_id: str, name: str, port: int, addresses: list[str]) -> None:
        self.agent_id = agent_id
        self.name = name
        self.port = port
        self.addresses = [a for a in addresses if a and not a.startswith("127.")]
        self._zc: Zeroconf | None = None
        self._info: ServiceInfo | None = None

    def start(self) -> bool:
        if not self.addresses:
            log.warning("mDNS: no LAN address to advertise")
            return False
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in self.name)[:40] or "pc"
        self._info = ServiceInfo(
            SERVICE_TYPE,
            f"{safe}-{self.agent_id[:6]}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(a) for a in self.addresses],
            port=self.port,
            properties={"id": self.agent_id, "name": self.name[:40], "v": str(P.PROTOCOL_VERSION)},
            server=f"{safe}.local.",
        )
        try:
            # Bind mDNS to the advertised LAN interfaces only.
            self._zc = Zeroconf(interfaces=self.addresses, ip_version=IPVersion.V4Only)
            self._zc.register_service(self._info, allow_name_change=True)
        except OSError as e:
            log.error("mDNS advertisement failed: %s", e)
            self._zc = None
            return False
        log.info("mDNS: advertising %s on %s:%d", self._info.name, ",".join(self.addresses), self.port)
        return True

    def stop(self) -> None:
        if self._zc and self._info:
            try:
                self._zc.unregister_service(self._info)
            finally:
                self._zc.close()
        self._zc = None
