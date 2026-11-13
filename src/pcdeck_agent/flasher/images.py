"""Checks that a merged firmware image is a sound image for a board's chip, before it is bundled
(``tools/fetch_firmware.py`` fails the build) and before it is flashed.

A merged image (from 0x0) holds the bootloader (at the chip's bootloader offset), the partition
table at 0x8000 and the app at 0x10000. Bootloader and app must be ESP images for this chip (image
chip id), with a valid checksum and, when they carry one, a matching appended SHA-256.
"""

from __future__ import annotations

from .boards import APP_OFFSET, Board

PARTITION_TABLE_OFFSET = 0x8000
PARTITION_MAGIC = b"\xaa\x50"


class ImageError(Exception):
    pass


def esptool_chip(board: Board) -> str:
    """esptool's chip key: "ESP32-S3" -> "esp32s3"."""
    return board.chip.lower().replace("-", "")


def check(data: bytes, board: Board) -> None:
    """Raise ImageError unless ``data`` is a sound merged image for ``board``."""
    from esptool.bin_image import LoadFirmwareImage
    from esptool.targets import CHIP_DEFS

    chip = esptool_chip(board)
    rom = CHIP_DEFS.get(chip)
    if rom is None:
        raise ImageError(f"esptool does not know {board.chip}")
    if len(data) <= APP_OFFSET:
        raise ImageError(f"{len(data)} bytes: no app at 0x{APP_OFFSET:x}")
    if data[PARTITION_TABLE_OFFSET : PARTITION_TABLE_OFFSET + 2] != PARTITION_MAGIC:
        raise ImageError("no partition table at 0x8000")
    parts = {"bootloader": data[rom.BOOTLOADER_FLASH_OFFSET : PARTITION_TABLE_OFFSET], "app": data[APP_OFFSET:]}
    for name, part in parts.items():
        if part[:1] != b"\xe9":
            raise ImageError(f"{name}: no ESP image header")
        try:
            img = LoadFirmwareImage(chip, part)
        except Exception as e:  # noqa: BLE001 (esptool raises FatalError and friends)
            raise ImageError(f"{name}: {e}") from e
        if img.chip_id != rom.IMAGE_CHIP_ID:
            raise ImageError(f"{name}: built for chip id {img.chip_id}, {board.chip} is {rom.IMAGE_CHIP_ID}")
        if img.checksum != img.calculate_checksum():
            raise ImageError(f"{name}: bad checksum")
        if img.append_digest and img.stored_digest != img.calc_digest:
            raise ImageError(f"{name}: SHA-256 digest mismatch")
