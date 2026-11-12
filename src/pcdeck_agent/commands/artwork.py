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


def _font_or_none(names: tuple[str, ...], size: int):
    from PIL import ImageFont

    for n in names:
        try:
            return ImageFont.truetype(n, size)
        except OSError:
            continue
    return None


def _font(names: tuple[str, ...], size: int):
    from PIL import ImageFont

    return _font_or_none(names, size) or ImageFont.load_default(size)


# Windows fonts first (unchanged output there); Linux (DejaVu) and macOS (Arial) fallbacks after them.
_BOLD = ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf", "Arial Bold.ttf")
_LABEL = ("seguisb.ttf", "segoeuib.ttf", "arial.ttf", "DejaVuSans-Bold.ttf", "Arial Bold.ttf")


def _hex(c: str) -> tuple[int, int, int]:
    return int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)


def service_tile(service, size: int = ART_SIZE) -> Artwork:
    """Generated cover for a known online service (brand gradient + icon/letter + name).

    Drawn at 4x and downscaled for smooth edges. Uses Windows' own fonts (Segoe UI, Segoe Fluent
    Icons / MDL2 Assets); on Linux/macOS DejaVu / Arial and the service's initial instead of the icon."""
    from PIL import Image, ImageDraw

    big = size * 4
    stops = [_hex(c) for c in service.colors]
    # Diagonal multi-stop gradient.
    grad = Image.new("RGB", (big, big))
    px = grad.load()
    n = len(stops) - 1
    for y in range(big):
        for x in range(0, big):
            t = (x + y) / (2 * (big - 1))
            k = min(int(t * n), n - 1)
            f = t * n - k
            a, b = stops[k], stops[k + 1]
            px[x, y] = (int(a[0] + (b[0] - a[0]) * f), int(a[1] + (b[1] - a[1]) * f), int(a[2] + (b[2] - a[2]) * f))
    d = ImageDraw.Draw(grad)
    white = (255, 255, 255)
    cx, cy = big // 2, int(big * 0.44)
    icons = None if service.letter else _font_or_none(("SegoeIcons.ttf", "segmdl2.ttf"), int(big * 0.40))
    if icons is not None:
        d.text((cx, cy), service.icon, font=icons, fill=white, anchor="mm")
    else:  # letter mark (Netflix "N"), or the service's initial where the Segoe icon fonts don't exist
        f = _font(_BOLD, int(big * 0.52))
        letter = service.letter or service.name[0]
        d.text((cx, cy), letter, font=f, fill=(229, 9, 20) if service.name == "Netflix" else white, anchor="mm")
    label = _font(_LABEL, int(big * 0.115))
    d.text((cx, int(big * 0.84)), service.name, font=label, fill=white, anchor="mm")
    img = grad.resize((size, size), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, "PNG")
    art = process(buf.getvalue(), size)
    # Stable id per service; the accent is the brand's main colour, darkened like any cover.
    return Artwork(zlib.crc32(b"svc:" + service.name.encode()) or 1, art.width, art.height, art.rgb565be, art.accent)


def chunks(art: Artwork) -> list[bytes]:
    total = len(art.rgb565be)
    return [
        HEADER.pack(MAGIC, art.art_id, art.width, art.height, off, total) + art.rgb565be[off : off + CHUNK]
        for off in range(0, total, CHUNK)
    ]
