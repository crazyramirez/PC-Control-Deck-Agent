"""App self-update in the tray app: periodic checks, the automatic install, the window's status line.

The network and file work runs on worker threads (``pcdeck_agent.updater``); results come back
through Qt signals. An automatic update waits while a deck is being flashed over USB, and an
attempt that did not end on the new version is not retried automatically for a day.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices

from .. import __version__, updater
from ..config import AgentConfig, save_config
from ..flasher.releases import ReleaseError
from ..updater import Update, UpdateError
from .strings import t

log = logging.getLogger("pcdeck_agent.gui")

FIRST_CHECK_MS = 60 * 1000  # let the app settle (and the network come up at logon) first
CHECK_EVERY_MS = 6 * 3600 * 1000
BUSY_RETRY_MS = 10 * 60 * 1000


class UpdateController(QObject):
    status = Signal(str, str)  # status line, button label ("" hides the button)
    _checked = Signal(object)  # Update | None | Exception
    _fetched = Signal(object, object)  # Update, Path | Exception
    _progress = Signal(int)  # download percent

    def __init__(
        self,
        cfg: AgentConfig,
        busy: Callable[[], bool],
        notify: Callable[[str], None],
        quit_app: Callable[[], None],
    ) -> None:
        super().__init__()
        self.cfg = cfg
        self.busy = busy
        self.notify = notify
        self.quit_app = quit_app
        self.kind = updater.install_kind()
        self.update: Update | None = None
        self.state = "idle"  # idle / checking / downloading / installing
        self._manual = False
        self._pending_v = ""
        self._checked.connect(self._on_checked)
        self._fetched.connect(self._on_fetched)
        self._progress.connect(lambda pct: self._show(t("update_downloading", v=self._pending_v, pct=pct), ""))
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.check)

    # ---- lifecycle ------------------------------------------------------------------------------
    def start(self) -> None:
        """First run of a new version: say so (and clear a pending attempt); then check now and then."""
        if self.cfg.last_version and updater.is_newer(__version__, self.cfg.last_version):
            self.notify(t("update_installed", v=__version__))
        if self.cfg.last_version != __version__ or self.cfg.update_attempt_version == __version__:
            self.cfg.last_version = __version__
            if self.cfg.update_attempt_version == __version__:
                self.cfg.update_attempt_version, self.cfg.update_attempt_at = "", 0.0
            save_config(self.cfg)
        self._show(t("update_current", v=__version__), t("update_check"))
        QTimer.singleShot(FIRST_CHECK_MS, self.check)
        self.timer.start(CHECK_EVERY_MS)

    def set_auto(self, on: bool) -> None:
        self.cfg.auto_update = on
        save_config(self.cfg)
        log.info("automatic app updates %s", "on" if on else "off")
        if on and self.update and self.update.asset:
            self._install(manual=False)

    # ---- checking -------------------------------------------------------------------------------
    def check(self, manual: bool = False) -> None:
        if self.state != "idle":
            return
        self.state = "checking"
        self._manual = manual
        if manual:
            self._show(t("update_checking"), "")

        def work() -> None:
            try:
                res: object = updater.check()
            except (ReleaseError, OSError) as e:
                res = e
            self._checked.emit(res)

        threading.Thread(target=work, name="app-update-check", daemon=True).start()

    def _on_checked(self, res: object) -> None:
        self.state = "idle"
        if isinstance(res, Exception):
            log.info("update check failed: %s", res)
            if self._manual:
                self._show(t("update_offline"), t("update_check"))
            return
        self.update = res  # type: ignore[assignment]
        if res is None:
            self._show(t("update_current", v=__version__), t("update_check"))
            return
        u: Update = res  # type: ignore[assignment]
        log.info("app update available: %s -> %s (%s)", __version__, u.version, u.asset.name if u.asset else "manual")
        if u.asset is None or not updater.can_replace(self.kind):
            self._show(t("update_manual", v=u.version), t("update_open_page"))
            return
        failed = updater.recently_failed(u.version, self.cfg.update_attempt_version, self.cfg.update_attempt_at, time.time())
        if self.cfg.auto_update and not failed:
            self._install(manual=False)
        elif failed:
            self._show(t("update_retry", v=u.version), t("update_now", v=u.version))
        else:
            self._show(t("update_available", v=u.version), t("update_now", v=u.version))

    # ---- the window's button --------------------------------------------------------------------
    def on_button(self) -> None:
        u = self.update
        if u and (u.asset is None or not updater.can_replace(self.kind)):
            QDesktopServices.openUrl(QUrl(updater.RELEASES_URL))
        elif u:
            self._install(manual=True)
        else:
            self.check(manual=True)

    # ---- installing -----------------------------------------------------------------------------
    def _install(self, manual: bool) -> None:
        u = self.update
        if self.state != "idle" or u is None or u.asset is None:
            return
        if self.busy():  # a deck is being flashed over USB: not now
            self._show(t("update_waiting", v=u.version), "")
            QTimer.singleShot(BUSY_RETRY_MS, lambda: self._install(manual))
            return
        self.state = "downloading"
        self._pending_v = u.version
        self._show(t("update_downloading", v=u.version, pct=0), "")
        last = [-1]

        def progress(done: int, total: int) -> None:
            pct = int(done * 100 / total) if total else 0
            if pct != last[0]:
                last[0] = pct
                self._progress.emit(pct)

        def work() -> None:
            try:
                res: object = updater.fetch(u, progress)
            except (ReleaseError, UpdateError, OSError) as e:
                res = e
            self._fetched.emit(u, res)

        threading.Thread(target=work, name="app-update-fetch", daemon=True).start()

    def _on_fetched(self, u: Update, res: object) -> None:
        if isinstance(res, Exception):
            self.state = "idle"
            log.warning("update download failed: %s", res)
            self._show(t("update_failed", msg=res), t("update_now", v=u.version))
            return
        self.state = "installing"
        self.cfg.update_attempt_version, self.cfg.update_attempt_at = u.version, time.time()
        save_config(self.cfg)
        try:
            updater.apply(Path(res), self.kind)  # type: ignore[arg-type]
        except UpdateError as e:
            self.state = "idle"
            self._show(t(e.key, **{"v": u.version, **e.params}), t("update_now", v=u.version))
            return
        except Exception as e:  # noqa: BLE001
            self.state = "idle"
            log.exception("update apply failed")
            self._show(t("update_failed", msg=e), t("update_now", v=u.version))
            return
        self._show(t("update_installing", v=u.version), "")
        self.notify(t("update_installing", v=u.version))
        log.info("updating to %s: quitting so the new version can take over", u.version)
        QTimer.singleShot(1500, self.quit_app)

    def _show(self, text: str, button: str) -> None:
        self.status.emit(text, button)
