"""Find decks on USB: Espressif serial ports, and what the deck's console says it is.

Probing only talks to the running firmware (``id``, else ``status``) with DTR / RTS held low, so
the chip is not reset. A port that does not answer is a board without the deck firmware, or one
that is busy / asleep; esptool decides what chip it is when the user flashes it.
"""

from __future__ import annotations

import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .boards import Board, by_key

ESPRESSIF_VID = 0x303A  # USB-Serial/JTAG of the ESP32-S3 / ESP32-P4 (most decks' USB-C)
# USB-UART bridges in front of UART0 on decks whose S3 USB pins drive the panel (ESP32-4848S040:
# CH340). Their DTR / RTS drive the board's auto-reset circuit; released, it does not reset.
USB_UART_BRIDGES = frozenset({
    (0x1A86, 0x7523),  # WCH CH340
    (0x1A86, 0x55D3),  # WCH CH343
    (0x10C4, 0xEA60),  # Silicon Labs CP210x
})
BAUD = 115200

_ID = re.compile(r"pcdeck board=([A-Za-z0-9_-]+) fw=(\d+\.\d+\.\d+)")
# "fw 0.12.3 | demo off | ..." (a log line may end right before it, so no anchor).
_FW = re.compile(r"fw (\d+\.\d+\.\d+) \| demo")
_GT911 = re.compile(r"touch GT911 .*?configured (\d+)x(\d+)")
_SCREEN_535 = re.compile(r"screen (?:on|off) \| touch presses=")


@dataclass(frozen=True)
class Port:
    device: str  # COM6, /dev/ttyACM0, /dev/cu.usbmodem1101
    serial_number: str  # the chip's MAC on Espressif USB-Serial/JTAG ("" behind most USB-UART bridges)
    description: str


@dataclass(frozen=True)
class Probe:
    port: Port
    responsive: bool  # the deck firmware's console answered
    board: Board | None = None
    fw: str | None = None
    error: str | None = None  # "busy" / "denied" / other text; the port could not be opened


def is_candidate(vid: int | None, pid: int | None) -> bool:
    """An Espressif USB-Serial/JTAG port, or a USB-UART bridge a deck may sit behind."""
    return vid == ESPRESSIF_VID or (vid, pid) in USB_UART_BRIDGES


def candidate_ports() -> list[Port]:
    """Serial ports that may be a deck (is_candidate), sorted by device name. A bridge port may be
    another device: probing only sends it the text commands ``id`` / ``status``, which a deck
    answers; nothing is flashed unless the user picks a deck."""
    from serial.tools import list_ports

    found = [
        Port(p.device, p.serial_number or "", p.description or "")
        for p in list_ports.comports()
        if is_candidate(p.vid, p.pid)
    ]
    return sorted(found, key=lambda p: p.device)


def identify(text: str) -> tuple[str | None, str | None]:
    """(board key, firmware version) from console output. ``id`` (firmware with the USB updater)
    names the board; older firmware is recognised from its ``status`` lines."""
    m = _ID.search(text)
    if m:
        return m.group(1), m.group(2)
    fw = _FW.search(text)
    if not fw:
        return None, None
    touch = _GT911.search(text)
    if touch:
        size = (int(touch.group(1)), int(touch.group(2)))
        key = {(1024, 600): "JC1060P470C", (480, 272): "JC4827W543"}.get(size)
        return key, fw.group(1)
    if _SCREEN_535.search(text):
        return "JC3248W535", fw.group(1)
    return None, fw.group(1)


class SerialLike(Protocol):
    def write(self, data: bytes) -> Any: ...
    def read(self, size: int = 1) -> bytes: ...
    def reset_input_buffer(self) -> None: ...


def ask(s: SerialLike, command: str, done: Callable[[str], bool], wait_s: float, settle_s: float = 0.4) -> str:
    """Send one console command and collect the reply until ``done(text)`` (plus ``settle_s`` for
    the lines after it) or ``wait_s``."""
    s.reset_input_buffer()
    s.write(command.encode() + b"\n")
    buf = bytearray()
    deadline = time.monotonic() + wait_s
    settled_at = None
    while time.monotonic() < deadline and (settled_at is None or time.monotonic() < settled_at):
        chunk = s.read(4096)
        if chunk:
            buf += chunk
            if settled_at is None and done(buf.decode("utf-8", "replace")):
                settled_at = time.monotonic() + settle_s
    return buf.decode("utf-8", "replace")


def port_error(e: BaseException) -> str:
    """``"denied"`` (Linux / macOS: the user may not open serial ports), ``"busy"`` (another
    program has the port; Windows says "access denied" for that) or the message itself."""
    msg = str(e)
    low = msg.lower()
    if sys.platform != "win32" and "permission denied" in low:
        return "denied"
    if any(k in low for k in ("access is denied", "acceso denegado", "permissionerror", "resource busy")):
        return "busy"
    return msg


def probe(port: Port, opener: Callable[[str], Any] | None = None) -> Probe:
    """Ask the firmware on ``port`` who it is. Never resets the chip."""
    try:
        s = opener(port.device) if opener else _open(port.device)
    except Exception as e:  # noqa: BLE001 (any open failure is reported, not raised)
        return Probe(port, False, error=port_error(e))
    try:
        text = ask(s, "id", lambda t: bool(_ID.search(t)), wait_s=1.2, settle_s=0.0)
        if not _ID.search(text):
            text += ask(s, "status", lambda t: bool(_FW.search(t)), wait_s=2.0)
    except Exception as e:  # noqa: BLE001
        return Probe(port, False, error=port_error(e))
    finally:
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass
    key, fw = identify(text)
    return Probe(port, fw is not None, by_key(key), fw)


def _open(device: str) -> Any:
    import serial

    s = serial.Serial()
    s.port = device
    s.baudrate = BAUD
    s.timeout = 0.1
    s.write_timeout = 1.0
    # USB-Serial/JTAG maps DTR / RTS to the chip's reset and boot pins: keep both released.
    s.dtr = False
    s.rts = False
    s.open()
    return s
