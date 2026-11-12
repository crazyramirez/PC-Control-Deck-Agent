"""Fold display strings to the glyphs the deck's fonts actually contain.

The firmware fonts (firmware/tools/gen_fonts.ps1) cover Latin-1, Latin Extended-A/B, Cyrillic,
Vietnamese and common typographic punctuation. Anything else (emoji, CJK, Greek, symbols) would be
drawn as empty boxes, so it is approximated here instead: accents are stripped when the base letter
exists, a few look-alikes are mapped and the rest is dropped.
"""

from __future__ import annotations

import unicodedata

# Exact glyph coverage of the deck text fonts (sizes 14/16/20), generated from
# firmware/src/ui/fonts/pcd_font_*.c (see firmware/tools/gen_fonts.ps1, $textExt).
# Regenerate when the font ranges change.
FONT_RANGES: tuple[tuple[int, int], ...] = (
    (0x20, 0x7E),
    (0xA0, 0x17F),
    (0x18F, 0x18F),
    (0x192, 0x192),
    (0x1A0, 0x1A1),
    (0x1AF, 0x1B0),
    (0x1B7, 0x1B7),
    (0x1C4, 0x1CE),
    (0x1D3, 0x1D4),
    (0x1E4, 0x1EB),
    (0x1EE, 0x1EF),
    (0x1FA, 0x21B),
    (0x21E, 0x21F),
    (0x22A, 0x22D),
    (0x230, 0x233),
    (0x237, 0x237),
    (0x400, 0x45F),
    (0x1EA0, 0x1EF9),
    (0x2010, 0x2010),
    (0x2012, 0x2015),
    (0x2018, 0x201A),
    (0x201C, 0x201E),
    (0x2020, 0x2022),
    (0x2026, 0x2026),
    (0x2030, 0x2030),
    (0x2032, 0x2033),
    (0x2039, 0x203A),
    (0x20AC, 0x20AC),
    (0x2122, 0x2122),
)

# Look-alikes for frequent characters outside the fonts.
_MAP = {
    "\u2011": "-", "\u2016": "|", "\u2017": "_", "\u201b": "'", "\u201f": '"',
    "\u2023": "\u2022", "\u2024": ".", "\u2025": "..", "\u2027": "\u00b7",
    "\u2031": "\u2030", "\u2034": "\u2033", "\u2035": "'", "\u2036": '"', "\u2037": '"',
    "\u2038": "^", "\u2116": "No", "\u3000": " ",
    "\u2190": "<-", "\u2192": "->", "\u2194": "<->", "\u2605": "*", "\u2606": "*",
}


def _in_font(ch: str) -> bool:
    o = ord(ch)
    return any(a <= o <= b for a, b in FONT_RANGES)


def fit_text(s: str) -> str:
    """Return ``s`` using only glyphs the deck can draw; whitespace is collapsed."""
    out: list[str] = []
    for ch in unicodedata.normalize("NFC", s):
        if _in_font(ch):
            out.append(ch)
            continue
        if ch in _MAP:
            out.append(_MAP[ch])
            continue
        # Decompose (e.g. Greek tonos, fullwidth forms, ligatures) and keep what is drawable.
        folded = "".join(c for c in unicodedata.normalize("NFKD", ch) if _in_font(c) and not unicodedata.combining(c))
        if folded:
            out.append(folded)
        elif unicodedata.category(ch).startswith("Z"):
            out.append(" ")
    text = " ".join("".join(out).split())
    # Separators left dangling by dropped emoji, e.g. "Song - 🔥" -> "Song".
    return text.strip(" -–—|·•")
