"""Album art for the deck: square crop, resize, RGB565 big-endian (the panel's native LVGL format,
LV_COLOR_16_SWAP=1) plus an accent colour for the player background. Pure function, no I/O."""

from __future__ import annotations

import colorsys
import io
import struct
import zlib
from dataclasses import dataclass

ART_SIZE = 150
MAGIC = b"PCDA"
HEADER = struct.Struct("<4sIHHII")  # magic, art_id, width, height, offset, total
CHUNK = 12 * 1024  # stays under the deck WebSocket library's 15 KiB frame limit


@dataclass(frozen=True)
class Artwork:
    art_id: int  # non-zero, stable for identical images
    width: int
    height: int
    rgb565be: bytes
    accent: tuple[int, int, int]

    @property
    def accent_hex(self) -> str:
        return "#{:02X}{:02X}{:02X}".format(*self.accent)


def _accent(img) -> tuple[int, int, int]:
    """Most vivid of the average colours of a 4x4 grid; darkened enough to sit behind white text."""
    small = img.resize((4, 4))
    best, best_score = (40, 40, 60), -1.0
    px = small.tobytes()  # RGB triplets (stable API across Pillow versions)
    for i in range(0, len(px), 3):
        r, g, b = px[i], px[i + 1], px[i + 2]
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        score = s * 0.7 + v * 0.3
        if score > best_score:
            best_score, best = score, (h, s, v)
    h, s, v = best
    r, g, b = colorsys.hsv_to_rgb(h, min(1.0, s * 1.1), min(v, 0.55))
    return int(r * 255), int(g * 255), int(b * 255)


def process(image_bytes: bytes, size: int = ART_SIZE) -> Artwork:
    from PIL import Image

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = img.size
    s = min(w, h)
    img = img.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s)).resize((size, size), Image.LANCZOS)
    raw = img.tobytes()
    out = bytearray(size * size * 2)
    j = 0
    for i in range(0, len(raw), 3):
        v = ((raw[i] & 0xF8) << 8) | ((raw[i + 1] & 0xFC) << 3) | (raw[i + 2] >> 3)
        out[j] = v >> 8  # big-endian
        out[j + 1] = v & 0xFF
        j += 2
    art_id = zlib.crc32(image_bytes) or 1
    return Artwork(art_id, size, size, bytes(out), _accent(img))


def chunks(art: Artwork) -> list[bytes]:
    total = len(art.rgb565be)
    return [
        HEADER.pack(MAGIC, art.art_id, art.width, art.height, off, total) + art.rgb565be[off : off + CHUNK]
        for off in range(0, total, CHUNK)
    ]
