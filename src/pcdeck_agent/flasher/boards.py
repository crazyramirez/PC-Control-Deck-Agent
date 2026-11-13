"""The deck models the USB updater knows: chip, firmware repository and release image names."""

from __future__ import annotations

import re
from dataclasses import dataclass

OWNER = "crazyramirez"
# One image per variant is enough: the release's merged image (bootloader + partition table + app,
# from 0x0) holds the app image byte for byte from 0x10000. A clean install writes all of it at 0x0;
# an update writes only the app part at 0x10000, so the deck's settings (NVS, below it) stay.
MERGED_OFFSET = 0x0
APP_OFFSET = 0x10000


@dataclass(frozen=True)
class Board:
    """One deck model. Adding a model = one entry in BOARDS; its firmware must answer the console
    command ``id`` and its CI publish ``pcdeck-<variant>-<version>-merged.bin`` per release."""

    key: str  # model id, as the deck's console reports it (`id`)
    name: str  # shown to the user
    chip: str  # esptool's CHIP_NAME: "ESP32-S3" / "ESP32-P4"
    repo: str  # GitHub repository whose releases carry the firmware
    # Firmware variants (PlatformIO environments) by the lowest chip major revision they run on.
    variants: tuple[tuple[str, int], ...] = (("deck", 0),)

    def env(self, chip_major_rev: int | None) -> str:
        """The variant for a chip revision (the first one while the revision is unknown)."""
        if chip_major_rev is None:
            return self.variants[0][0]
        fits = [(rev, env) for env, rev in self.variants if rev <= chip_major_rev]
        return max(fits)[1] if fits else self.variants[0][0]

    def image(self, version: str, env: str) -> str:
        """Name of the merged image in a release, as the firmware CI publishes it."""
        return f"pcdeck-{env}-{version}-merged.bin"


BOARDS: tuple[Board, ...] = (
    # ESP32-P4 silicon v1.x and v3.x need different builds.
    Board("JC1060P470C", 'Guition JC1060P470C · 7"', "ESP32-P4", "PC-Control-Deck-ESP32-JC1060P470C",
          variants=(("deck", 0), ("deck_r3", 3))),  # fmt: skip
    Board("JC4827W543", 'Guition JC4827W543 · 4.3"', "ESP32-S3", "PC-Control-Deck-ESP32-JC4827W543"),
    Board("JC3248W535", 'Guition JC3248W535 · 3.5"', "ESP32-S3", "PC-Control-Deck-ESP32-JC3248W535"),
)


def by_key(key: str | None) -> Board | None:
    return next((b for b in BOARDS if b.key == key), None)


def for_chip(chip: str | None) -> list[Board]:
    """Models a chip can be (all of them when the chip is not known yet)."""
    return [b for b in BOARDS if chip is None or b.chip == chip]


_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def parse_version(text: str | None) -> tuple[int, int, int] | None:
    """``"v0.12.3"`` / ``"0.12.3"`` -> ``(0, 12, 3)``; anything else -> None."""
    m = _VERSION.match((text or "").strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def is_newer(latest: str | None, installed: str | None) -> bool:
    a, b = parse_version(latest), parse_version(installed)
    return a is not None and (b is None or a > b)
