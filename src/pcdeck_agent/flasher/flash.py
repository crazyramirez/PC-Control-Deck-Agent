"""One firmware update over USB, with esptool's Python API.

connect (esptool resets the chip into its ROM bootloader) -> check the chip is the model's (S3 /
P4) and pick the image variant (ESP32-P4 silicon v1.x / v3.x) -> the release's merged image
(bundled, or downloaded when GitHub has a newer one; verified) -> load the flasher stub ->
[erase everything] -> write (esptool verifies the MD5) -> hard reset.

``clean=False`` writes the app part of the image (from 0x10000) at 0x10000: bootloader, partition
table and NVS (Wi-Fi, pairing, settings) stay. ``clean=True`` erases the flash and writes the
whole image at 0x0.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from . import images
from .boards import APP_OFFSET, MERGED_OFFSET, Board
from .probe import port_error
from .releases import Release, ReleaseError, cache_dir, image_path

log = logging.getLogger("pcdeck.flasher")


class FlashError(Exception):
    """A failure with a UI string key (``gui/strings.py``) and its parameters."""

    def __init__(self, key: str, **params: object) -> None:
        super().__init__(key, params)
        self.key = key
        self.params = params


@dataclass(frozen=True)
class Result:
    version: str
    image: str
    chip: str  # "ESP32-P4 (revision v1.3)"


# stage(key) with keys: connecting, downloading, preparing, erasing, writing, restarting.
Stage = Callable[[str], None]
Progress = Callable[[float], None]  # 0.0 .. 1.0 within the current stage


@contextmanager
def _esptool_output(progress: Progress) -> Iterator[None]:
    """Route esptool's console output to our log and its progress bars to ``progress``."""
    from esptool.logger import EspLog, EsptoolLogger, TemplateLogger

    class _Sink(TemplateLogger):
        def print(self, *args: object, **kwargs: object) -> None:
            text = " ".join(str(a) for a in args).strip()
            if text:
                log.info("esptool: %s", text)

        def note(self, message: str) -> None:
            log.info("esptool note: %s", message)

        def warning(self, message: str) -> None:
            log.warning("esptool: %s", message)

        def error(self, message: str) -> None:
            log.error("esptool: %s", message)

        def stage(self, finish: bool = False) -> None:
            pass

        def progress_bar(self, cur_iter: int, total_iters: int, prefix: str = "", suffix: str = "", bar_length: int = 30) -> None:
            if total_iters > 0:
                progress(min(1.0, cur_iter / total_iters))

        def set_verbosity(self, verbosity: str) -> None:
            pass

    # set_logger wraps a TemplateLogger for esp-pylib and stores it in EspLog.instance (the global
    # that esptool's `log` proxy reads); the wrapper itself has no set_logger, so restore directly.
    previous = EspLog.instance
    EsptoolLogger.set_logger(previous, _Sink())
    try:
        yield
    finally:
        EspLog.instance = previous


def writes(image: bytes, clean: bool) -> list[tuple[int, bytes]]:
    """(offset, data) to write from a merged image: all of it, or only its app part."""
    if len(image) <= APP_OFFSET:
        raise FlashError("flash_failed", msg=f"image too small ({len(image)} bytes)")
    return [(MERGED_OFFSET, image)] if clean else [(APP_OFFSET, image[APP_OFFSET:])]


def _chip_description(esp: object) -> str:
    try:
        return str(esp.get_chip_description())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return str(getattr(esp, "CHIP_NAME", "?"))


def update(port: str, board: Board, release: Release, clean: bool, stage: Stage, progress: Progress) -> Result:
    """Flash ``release`` for ``board`` on ``port``. Raises FlashError."""
    from esptool.cmds import attach_flash, detect_chip, erase_flash, reset_chip, run_stub, write_flash

    with _esptool_output(progress):
        stage("connecting")
        try:
            esp = detect_chip(port, connect_attempts=4)
        except Exception as e:  # noqa: BLE001 (serial and esptool errors alike)
            why = port_error(e)
            log.warning("connect %s failed: %s", port, e)
            raise FlashError({"busy": "flash_port_busy", "denied": "flash_port_denied"}.get(why, "flash_no_connect"), port=port, msg=why) from e
        try:
            chip = esp.CHIP_NAME
            desc = _chip_description(esp)
            log.info("%s on %s: %s", board.key, port, desc)
            if chip != board.chip:
                raise FlashError("flash_wrong_chip", chip=chip, expected=board.chip, model=board.name)
            env = board.env(esp.get_major_chip_version())
            name = board.image(release.version, env)
            asset = release.assets.get(name)
            if asset is None:
                raise FlashError("flash_no_asset", name=name, v=release.version)

            if asset.path is None:
                stage("downloading")
            try:
                path = image_path(asset, cache_dir(board), progress=lambda done, total: progress(done / total if total else 0.0))
            except ReleaseError as e:
                raise FlashError("flash_download_failed", msg=str(e)) from e
            image = path.read_bytes()
            try:
                images.check(image, board)
            except images.ImageError as e:
                raise FlashError("flash_bad_image", name=name, msg=str(e)) from e
            data = writes(image, clean)

            stage("preparing")
            esp = run_stub(esp)
            attach_flash(esp)
            if clean:
                stage("erasing")
                erase_flash(esp)
            stage("writing")
            progress(0.0)
            write_flash(esp, data)
            stage("restarting")
            reset_chip(esp, "hard-reset")
            log.info("flashed %s %s (%s, %s) to %s on %s", release.source, name, release.version,
                     "clean" if clean else "app only", board.key, port)  # fmt: skip
            return Result(release.version, name, desc)
        except FlashError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("flashing %s on %s failed", board.key, port)
            raise FlashError("flash_failed", msg=str(e) or type(e).__name__) from e
        finally:
            try:
                esp._port.close()  # noqa: SLF001 (esptool keeps the serial port here)
            except Exception:  # noqa: BLE001
                pass
