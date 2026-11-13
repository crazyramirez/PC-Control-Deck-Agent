"""USB firmware updater: the decks plugged into this computer, each updatable to its latest release.

Scanning (every few seconds while the dialog is open), the release lookup and the update itself
run on worker threads and report back through Qt signals; see ``pcdeck_agent.flasher``.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..flasher import boards, bundled, flash, probe, releases
from ..flasher.boards import Board
from ..flasher.flash import FlashError
from ..flasher.probe import Probe
from ..flasher.releases import Release
from .icons import app_icon
from .strings import t
from .window import CONTENT_WIDTH, QSS, _muted

EXTRA_QSS = """
QDialog#root { background: #090D15; }
QComboBox { background: #1F2A3C; color: #F5F8FF; border: 1px solid #304056; border-radius: 8px;
            padding: 6px 10px; font-size: 13px; }
QComboBox QAbstractItemView { background: #171F2D; color: #F5F8FF; selection-background-color: #26344A; }
QPushButton:disabled { color: #5B6474; border-color: #26344A; }
QLabel#error { color: #F87171; }
QLabel#ok { color: #68E5A0; font-weight: 600; }
"""

SCAN_MS = 3000
REPROBE_AFTER_FLASH_MS = 7000  # the deck reboots and its USB port comes back


class _Signals(QObject):
    scanned = Signal(object)  # dict[device, Probe]
    releases = Signal(object)  # dict[board key, Release | str]
    stage = Signal(str, str)  # device, stage key
    progress = Signal(str, float)  # device, 0..1
    finished = Signal(str, object)  # device, flash.Result | FlashError


class DeckRow(QFrame):
    """One USB port with a deck (or an unidentified ESP32) on it."""

    flash_clicked = Signal(str)

    def __init__(self, device: str) -> None:
        super().__init__()
        self.device = device
        self.probe: Probe | None = None
        self.release: Release | str | None = None
        self.setObjectName("card")
        self.setFixedWidth(CONTENT_WIDTH)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)

        top = QHBoxLayout()
        self.name = QLabel()
        f = QFont()
        f.setBold(True)
        self.name.setFont(f)
        top.addWidget(self.name, 1)
        top.addWidget(_muted(device))
        lay.addLayout(top)
        self.info = _muted()
        lay.addWidget(self.info)

        self.model_row = QWidget()
        mr = QHBoxLayout(self.model_row)
        mr.setContentsMargins(0, 0, 0, 0)
        mr.addWidget(_muted(t("flash_model")))
        self.model = QComboBox()
        self.model.currentIndexChanged.connect(lambda _i: self.refresh())
        mr.addWidget(self.model, 1)
        lay.addWidget(self.model_row)

        self.clean = QCheckBox(t("flash_clean"))
        lay.addWidget(self.clean)
        self.clean_note = _muted(t("flash_clean_forced"))
        lay.addWidget(self.clean_note)

        self.button = QPushButton()
        self.button.setObjectName("primary")
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.button.clicked.connect(lambda: self.flash_clicked.emit(self.device))
        lay.addWidget(self.button)

        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        self.bar.setRange(0, 1000)
        self.bar.hide()
        lay.addWidget(self.bar)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.hide()
        lay.addWidget(self.status)
        self.busy = False

    # ---- state ---------------------------------------------------------------------------------
    def board(self) -> Board | None:
        if self.probe and self.probe.board:
            return self.probe.board
        key = self.model.currentData()
        return boards.by_key(key) if key else None

    def set_probe(self, p: Probe) -> None:
        self.probe = p
        need_model = p.board is None and p.error is None
        if need_model and self.model.count() == 0:
            self.model.blockSignals(True)
            self.model.addItem(t("flash_pick_model"), None)
            for b in boards.BOARDS:
                self.model.addItem(b.name, b.key)
            self.model.blockSignals(False)
        self.model_row.setVisible(need_model)
        self.refresh()

    def set_release(self, rel: Release | str | None) -> None:
        self.release = rel
        self.refresh()

    def refresh(self) -> None:
        p = self.probe
        if p is None:
            return
        board = self.board()
        self.name.setText(p.board.name if p.board else t("flash_unknown"))
        rel = self.release if board else None
        forced = not p.responsive
        self.clean.setVisible(p.responsive and p.error is None)
        self.clean_note.setVisible(forced and p.error is None)

        if p.error:
            key = {"busy": "flash_port_busy", "denied": "flash_port_denied"}.get(p.error)
            self.info.setText(t(key, port=self.device) if key else p.error)
        elif not p.responsive:
            self.info.setText(t("flash_no_fw"))
        elif isinstance(rel, Release):
            latest = t("flash_up_to_date") if not boards.is_newer(rel.version, p.fw) else t("flash_latest", v=rel.version)
            self.info.setText(f"{t('flash_fw', fw=p.fw)} · {latest}")
        else:
            self.info.setText(t("flash_fw", fw=p.fw))

        if isinstance(rel, str):
            self.info.setText(t("flash_release_err", msg=rel))
        if isinstance(rel, Release) and not p.error:
            if not p.responsive:
                label = t("flash_install", v=rel.version)
            elif boards.is_newer(rel.version, p.fw):
                label = t("flash_update_to", v=rel.version)
            else:
                label = t("flash_reinstall", v=rel.version)
            self.button.setText(label)
            self.button.setEnabled(not self.busy)
        else:
            # Not possible yet: busy port, model not picked, version still being looked up / failed.
            waiting = rel is None and board is not None and not p.error
            self.button.setText(t("flash_checking") if waiting else t("flash_install", v="").strip())
            self.button.setEnabled(False)

    def wants_clean(self) -> bool:
        return bool(self.probe and (not self.probe.responsive or self.clean.isChecked()))

    def start(self) -> None:
        self.busy = True
        self.button.setEnabled(False)
        self.clean.setEnabled(False)
        self.model.setEnabled(False)
        self.bar.setValue(0)
        self.bar.show()
        self.status.setObjectName("")
        self.status.setStyleSheet("")
        self.status.show()

    def show_stage(self, key: str) -> None:
        self.status.setText(t(f"flash_stage_{key}"))
        self.bar.setValue(0)

    def show_progress(self, frac: float) -> None:
        self.bar.setValue(int(frac * 1000))

    def finish(self, ok: bool, text: str) -> None:
        self.busy = False
        self.bar.setVisible(False)
        self.clean.setEnabled(True)
        self.model.setEnabled(True)
        self.status.setText(text)
        self.status.setStyleSheet("color: #68E5A0; font-weight: 600;" if ok else "color: #F87171;")
        self.status.show()
        self.refresh()


class FlashDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("root")
        self.setWindowTitle(t("flash_title"))
        self.setWindowIcon(app_icon())
        self.setStyleSheet(QSS + EXTRA_QSS)
        self.sig = _Signals()
        self.sig.scanned.connect(self._on_scanned)
        self.sig.releases.connect(self._on_releases)
        self.sig.stage.connect(lambda dev, k: self.rows[dev].show_stage(k) if dev in self.rows else None)
        self.sig.progress.connect(lambda dev, f: self.rows[dev].show_progress(f) if dev in self.rows else None)
        self.sig.finished.connect(self._on_finished)
        self.rows: dict[str, DeckRow] = {}
        self.probes: dict[str, Probe] = {}  # device -> last probe (kept while the port stays)
        # Bundled firmware first (usable at once, offline); GitHub replaces it when it has a newer one.
        self.bundled = bundled.load()
        self.release_map: dict[str, Release | str] = dict(self.bundled)
        self.notes: dict[str, tuple[bool, str]] = {}  # device -> last update outcome (survives re-plugs)
        self.job: str | None = None  # device being flashed
        self.scanning = False

        root = QVBoxLayout(self)
        root.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(12)
        title = QLabel(t("flash_title"))
        title.setObjectName("title")
        root.addWidget(title)
        intro = _muted(t("flash_intro"))
        intro.setFixedWidth(CONTENT_WIDTH)
        root.addWidget(intro)
        self.list = QVBoxLayout()
        self.list.setSpacing(10)
        root.addLayout(self.list)
        self.empty = _muted(t("flash_scanning"))
        self.empty.setFixedWidth(CONTENT_WIDTH)
        root.addWidget(self.empty)
        foot = QHBoxLayout()
        foot.addStretch(1)
        close = QPushButton(t("flash_close"))
        close.clicked.connect(self.close)
        foot.addWidget(close)
        root.addLayout(foot)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._scan)
        self.timer.start(SCAN_MS)
        self._scan()
        threading.Thread(target=self._fetch_releases, name="fw-releases", daemon=True).start()

    # ---- workers ---------------------------------------------------------------------------------
    def _fetch_releases(self) -> None:
        out: dict[str, Release | str] = {}
        for b in boards.BOARDS:
            mine = self.bundled.get(b.key)
            try:
                online = releases.latest(b)
            except releases.ReleaseError as e:
                online = None
                if mine is None:
                    out[b.key] = str(e)  # nothing bundled and no internet: say why
                    continue
            out[b.key] = releases.pick(mine, online)  # type: ignore[assignment]
        self.sig.releases.emit(out)

    def _scan(self) -> None:
        if self.scanning or self.job:
            return
        self.scanning = True
        known = dict(self.probes)

        def work() -> None:
            result: dict[str, Probe] = {}
            try:
                for port in probe.candidate_ports():
                    old = known.get(port.device)
                    if old and old.responsive and old.port.serial_number == port.serial_number:
                        result[port.device] = old  # identified already: do not poke the console again
                    else:
                        result[port.device] = probe.probe(port)
            finally:
                self.sig.scanned.emit(result)

        threading.Thread(target=work, name="fw-scan", daemon=True).start()

    def _flash(self, device: str) -> None:
        row = self.rows.get(device)
        board = row.board() if row else None
        if row is None or board is None or self.job:
            return
        rel = self.release_map.get(board.key)
        if not isinstance(rel, Release):
            return
        clean = row.wants_clean()
        if row.probe and not row.probe.board:
            if QMessageBox.question(self, t("flash_title"), t("flash_confirm_model", model=board.name)) != QMessageBox.StandardButton.Yes:
                return
        elif clean and QMessageBox.question(self, t("flash_title"), t("flash_confirm_clean")) != QMessageBox.StandardButton.Yes:
            return
        self.job = device
        for r in self.rows.values():
            r.button.setEnabled(False)
        row.start()

        def work() -> None:
            try:
                res: object = flash.update(
                    device, board, rel, clean,
                    stage=lambda k: self.sig.stage.emit(device, k),
                    progress=lambda f: self.sig.progress.emit(device, f),
                )  # fmt: skip
            except FlashError as e:
                res = e
            except Exception as e:  # noqa: BLE001 (anything else is still reported in the row)
                res = FlashError("flash_failed", msg=str(e) or type(e).__name__)
            self.sig.finished.emit(device, res)

        threading.Thread(target=work, name="fw-flash", daemon=True).start()

    # ---- results ---------------------------------------------------------------------------------
    def _on_releases(self, rel: dict[str, Release | str]) -> None:
        self.release_map = rel
        for row in self.rows.values():
            b = row.board()
            row.set_release(rel.get(b.key) if b else None)

    def _on_scanned(self, result: dict[str, Probe]) -> None:
        self.scanning = False
        if self.job:
            return
        self.probes = result
        for dev in list(self.rows):
            if dev not in result:
                row = self.rows.pop(dev)
                self.list.removeWidget(row)
                row.deleteLater()
        for dev, p in result.items():
            row = self.rows.get(dev)
            if row is None:
                row = DeckRow(dev)
                row.flash_clicked.connect(self._flash)
                row.model.currentIndexChanged.connect(lambda _i, r=row: self._attach_release(r))
                self.rows[dev] = row
                self.list.addWidget(row)
                if dev in self.notes:
                    row.finish(*self.notes[dev])
            row.set_probe(p)
            self._attach_release(row)
        self.empty.setText(t("flash_none"))
        self.empty.setVisible(not self.rows)

    def _attach_release(self, row: DeckRow) -> None:
        b = row.board()
        row.set_release(self.release_map.get(b.key) if b else None)

    def _on_finished(self, device: str, res: object) -> None:
        self.job = None
        if isinstance(res, FlashError):
            note = (False, t(res.key, **{"port": device, **res.params}))
        else:
            note = (True, t("flash_done", v=res.version))  # type: ignore[attr-defined]
        self.notes[device] = note
        row = self.rows.get(device)
        if row is not None:
            row.finish(*note)
        for r in self.rows.values():
            r.refresh()
        # The deck reboots into the new firmware: probe it again once its port is back.
        self.probes.pop(device, None)
        QTimer.singleShot(REPROBE_AFTER_FLASH_MS, self._scan)

    # ---- window ----------------------------------------------------------------------------------
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt API)
        if self.job:
            QMessageBox.information(self, t("flash_title"), t("flash_wait"))
            event.ignore()
            return
        self.timer.stop()
        super().closeEvent(event)  # QDialog: reject() -> finished, so the main window drops it
