"""Main window: status, pairing code, paired devices, this-PC info, settings."""

from __future__ import annotations

import ctypes
import os
import sys
import time

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..config import log_dir
from ..runtime import AgentRuntime, RuntimeStatus
from ..security.pairing import PairingState
from ..server import SessionInfo
from . import autostart
from .icons import app_icon
from .strings import t

QSS = """
QWidget#root { background: #090D15; }
QLabel { color: #F5F8FF; font-size: 13px; }
QLabel[muted="true"] { color: #A8B5C9; }
QLabel#title { font-size: 20px; font-weight: 600; }
QLabel#cardTitle { font-size: 14px; font-weight: 600; color: #F5F8FF; }
QLabel#code { font-size: 44px; font-weight: 700; color: #52D5E8; letter-spacing: 6px; }
QLabel#chip { border-radius: 11px; padding: 3px 10px; font-size: 12px; font-weight: 600; }
QFrame#card { background: #171F2D; border: 1px solid #304056; border-radius: 12px; }
QPushButton { background: #1F2A3C; color: #F5F8FF; border: 1px solid #304056; border-radius: 8px;
              padding: 8px 14px; font-size: 13px; }
QPushButton:hover { background: #26344A; }
QPushButton:pressed { background: #304056; }
QPushButton#primary { background: #14424A; border-color: #52D5E8; font-weight: 600; padding: 10px 16px; }
QPushButton#primary:hover { background: #185560; }
QPushButton#danger { border-color: #F87171; color: #F87171; padding: 5px 10px; }
QPushButton#link { background: transparent; border: none; color: #A8B5C9; padding: 4px 6px; }
QPushButton#link:hover { color: #F5F8FF; }
QCheckBox { color: #F5F8FF; font-size: 13px; spacing: 8px; }
QCheckBox::indicator { width: 16px; height: 16px; border-radius: 4px; border: 1px solid #304056; background: #171F2D; }
QCheckBox::indicator:checked { background: #52D5E8; border-color: #52D5E8; }
QProgressBar { background: #304056; border: none; border-radius: 3px; height: 6px; }
QProgressBar::chunk { background: #52D5E8; border-radius: 3px; }
QScrollArea { border: none; background: transparent; }
"""

CHIP_COLORS = {"ok": ("#103A2A", "#68E5A0"), "idle": ("#1F2A3C", "#A8B5C9"), "error": ("#3A1717", "#F87171")}


class Bridge(QObject):
    """Re-emits runtime callbacks (runtime thread) as Qt signals (delivered on the GUI thread)."""

    status = Signal(object)
    sessions = Signal(object)
    paired = Signal(str, str)
    pairing = Signal(object)


CONTENT_WIDTH = 400  # fixed card width: keeps text wrapping stable and the window compact


def _card(title: str) -> tuple[QFrame, QVBoxLayout]:
    f = QFrame()
    f.setObjectName("card")
    f.setFixedWidth(CONTENT_WIDTH)
    lay = QVBoxLayout(f)
    lay.setContentsMargins(16, 14, 16, 14)
    lay.setSpacing(8)
    lbl = QLabel(title)
    lbl.setObjectName("cardTitle")
    lay.addWidget(lbl)
    return f, lay


def _muted(text: str = "") -> QLabel:
    lbl = QLabel(text)
    lbl.setProperty("muted", True)
    lbl.setWordWrap(True)
    return lbl


def _ago(ts: float) -> str:
    d = max(0, time.time() - ts)
    if d < 90:
        return t("just_now")
    if d < 3600:
        return t("minutes_ago", n=int(d // 60))
    if d < 86400:
        return t("hours_ago", n=int(d // 3600))
    return t("days_ago", n=int(d // 86400))


def is_admin() -> bool:
    if sys.platform != "win32":
        return hasattr(os, "geteuid") and os.geteuid() == 0  # Linux / macOS: root
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def open_logs() -> None:
    if sys.platform == "win32":
        os.startfile(str(log_dir()))
        return
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices

    log_dir().mkdir(parents=True, exist_ok=True)
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_dir())))


class MainWindow(QWidget):
    state_changed = Signal(str)  # "ok" / "idle" / "error" for the tray icon

    def __init__(self, runtime: AgentRuntime, bridge: Bridge) -> None:
        super().__init__()
        self.rt = runtime
        self.bridge = bridge
        self._status = RuntimeStatus()
        self._sessions: list[SessionInfo] = []
        self._pairing = PairingState(False, None, 0.0, 0)
        self._hidden_hint_shown = False
        self.setObjectName("root")
        self.setWindowTitle(f"{t('app')} {__version__}")
        self.setWindowIcon(app_icon())
        self.setStyleSheet(QSS)
        self._build()
        bridge.status.connect(self._on_status)
        bridge.sessions.connect(self._on_sessions)
        bridge.paired.connect(self._on_paired)
        bridge.pairing.connect(self._on_pairing)
        self._tick = QTimer(self)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start(1000)
        self._refresh_devices()
        self._refresh_header()

    # ---- layout ---------------------------------------------------------------------------------
    def _build(self) -> None:
        # Window size == content size: no scrolling, no empty space. SetFixedSize makes the window
        # follow the layout's size hint whenever something appears/disappears (pairing code, devices).
        root = QVBoxLayout(self)
        root.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)
        root.setContentsMargins(18, 16, 18, 12)
        root.setSpacing(12)

        # Header
        head = QHBoxLayout()
        icon = QLabel()
        icon.setPixmap(app_icon().pixmap(44, 44))
        head.addWidget(icon)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        title = QLabel(t("app"))
        title.setObjectName("title")
        self.subtitle = _muted(t("starting"))
        titles.addWidget(title)
        titles.addWidget(self.subtitle)
        head.addLayout(titles, 1)
        self.chip = QLabel(t("starting"))
        self.chip.setObjectName("chip")
        head.addWidget(self.chip, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(head)

        # Connect card
        card, lay = _card(t("connect_title"))
        self.steps = QLabel()
        self.steps.setWordWrap(True)
        self.steps.setTextFormat(Qt.TextFormat.RichText)
        self.steps.setProperty("muted", True)
        lay.addWidget(self.steps)
        self.pair_btn = QPushButton(t("pair_button"))
        self.pair_btn.setObjectName("primary")
        self.pair_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pair_btn.clicked.connect(self._start_pairing)
        lay.addWidget(self.pair_btn)
        self.code_box = QWidget()
        cb = QVBoxLayout(self.code_box)
        cb.setContentsMargins(0, 4, 0, 0)
        cb.setSpacing(6)
        self.code_lbl = QLabel("––– –––")
        self.code_lbl.setObjectName("code")
        self.code_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.code_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        cb.addWidget(self.code_lbl)
        self.code_bar = QProgressBar()
        self.code_bar.setTextVisible(False)
        self.code_bar.setFixedHeight(6)
        cb.addWidget(self.code_bar)
        row = QHBoxLayout()
        self.code_info = _muted()
        row.addWidget(self.code_info, 1)
        self.code_cancel = QPushButton(t("cancel"))
        self.code_cancel.clicked.connect(self._cancel_pairing)
        row.addWidget(self.code_cancel)
        cb.addLayout(row)
        self.code_box.hide()
        lay.addWidget(self.code_box)
        self.pair_result = QLabel()
        self.pair_result.setStyleSheet("color: #68E5A0; font-weight: 600;")
        self.pair_result.hide()
        lay.addWidget(self.pair_result)
        root.addWidget(card)

        # Devices card
        card, lay = _card(t("devices_title"))
        self.devices_box = QVBoxLayout()
        self.devices_box.setSpacing(6)
        lay.addLayout(self.devices_box)
        root.addWidget(card)

        # This PC card
        card, lay = _card(t("pc_title"))
        self.pc_rows: dict[str, QLabel] = {}
        for key in ("address", "bonjour", "sensors", "privileges"):
            r = QHBoxLayout()
            k = _muted(t(key))
            k.setFixedWidth(150)
            v = QLabel("–")
            v.setWordWrap(True)
            r.addWidget(k)
            r.addWidget(v, 1)
            lay.addLayout(r)
            self.pc_rows[key] = v
        self.notes = _muted()
        lay.addWidget(self.notes)
        self.allow_power = QCheckBox(t("allow_power"))
        self.allow_power.setChecked(self.rt.cfg.allow_power)
        self.allow_power.toggled.connect(self.rt.set_allow_power)
        lay.addWidget(self.allow_power)
        lay.addWidget(_muted(t("security_note")))
        root.addWidget(card)

        # Footer
        foot = QHBoxLayout()
        self.autostart = QCheckBox(t("autostart"))
        self.autostart.setChecked(autostart.is_enabled())
        self.autostart.toggled.connect(self._toggle_autostart)
        foot.addWidget(self.autostart, 1)
        version = _muted(f"v{__version__}")
        version.setToolTip(f"{t('app')} {__version__}")
        foot.addWidget(version)
        logs = QPushButton(t("logs"))
        logs.setObjectName("link")
        logs.clicked.connect(open_logs)
        foot.addWidget(logs)
        quit_btn = QPushButton(t("quit"))
        quit_btn.setObjectName("link")
        quit_btn.clicked.connect(self._quit)
        foot.addWidget(quit_btn)
        root.addLayout(foot)

    # ---- runtime events ---------------------------------------------------------------------------
    def _on_status(self, st: RuntimeStatus) -> None:
        self._status = st
        self._refresh_header()
        self.pc_rows["address"].setText(", ".join(st.endpoints) or "–")
        self.pc_rows["bonjour"].setText(t("on") if st.mdns else t("off"))
        self.pc_rows["sensors"].setText(st.provider or "–")
        self.pc_rows["privileges"].setText(t("admin_yes") if is_admin() else t("admin_no"))
        # Raw provider notes go to the log; the user gets one actionable sentence instead.
        self.notes.setText(t("admin_hint"))
        self.notes.setVisible(not is_admin())
        if st.error:
            self.pc_rows["address"].setText(st.error)

    def _on_sessions(self, sessions: list[SessionInfo]) -> None:
        self._sessions = sessions
        self._refresh_devices()
        self._refresh_header()

    def _on_paired(self, client_id: str, name: str) -> None:
        self.pair_result.setText(t("paired_ok", name=name))
        self.pair_result.show()
        self.code_box.hide()
        self.pair_btn.show()
        QTimer.singleShot(8000, self.pair_result.hide)
        self._refresh_devices()

    def _on_pairing(self, st: PairingState) -> None:
        self._pairing = st
        if st.active and st.code:
            self.code_lbl.setText(f"{st.code[:3]} {st.code[3:]}")
            self.code_box.show()
            self.pair_btn.hide()
            self.pair_result.hide()
        elif not self.pair_result.isVisible():
            self.code_box.hide()
            self.pair_btn.show()
        self._on_tick()

    # ---- refresh ------------------------------------------------------------------------------------
    def _overall(self) -> str:
        if self._status.error:
            return "error"
        if any(s.authenticated for s in self._sessions):
            return "ok"
        return "idle"

    def _refresh_header(self) -> None:
        state = self._overall()
        n = sum(1 for s in self._sessions if s.authenticated)
        if self._status.error:
            chip, sub = t("error"), self._status.error
        elif not self._status.running:
            chip, sub = t("starting"), t("starting")
        else:
            chip = t("running")
            sub = (t("n_connected", n=n) if n == 1 else t("n_connected_pl", n=n)) if n else t("none_connected")
        bg, fg = CHIP_COLORS[state]
        self.chip.setText(chip)
        self.chip.setStyleSheet(f"background:{bg}; color:{fg};")
        self.subtitle.setText(sub)
        self.steps.setText(t("connect_steps", pc=self.rt.cfg.device_id))
        self.state_changed.emit(state)

    def _refresh_devices(self) -> None:
        while self.devices_box.count():
            item = self.devices_box.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        devices = self.rt.devices()
        if not devices:
            self.devices_box.addWidget(_muted(t("no_devices")))
            return
        live = {s.client_id: s for s in self._sessions if s.authenticated}
        for d in devices:
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 2, 0, 2)
            dot = QLabel("●")
            dot.setStyleSheet(f"color: {'#68E5A0' if d.client_id in live else '#4A5870'}; font-size: 14px;")
            h.addWidget(dot)
            text = QVBoxLayout()
            text.setSpacing(0)
            name = QLabel(d.name)
            f = QFont()
            f.setBold(True)
            name.setFont(f)
            if d.client_id in live:
                sub = t("connected_now", ip=live[d.client_id].peer)
                if live[d.client_id].fw:
                    sub += f" · firmware {live[d.client_id].fw}"
            elif d.last_seen:
                sub = t("last_seen", when=_ago(d.last_seen))
            else:
                sub = t("never_seen")
            text.addWidget(name)
            text.addWidget(_muted(sub))
            h.addLayout(text, 1)
            btn = QPushButton(t("unpair"))
            btn.setObjectName("danger")
            btn.clicked.connect(lambda _=False, cid=d.client_id, nm=d.name: self._unpair(cid, nm))
            h.addWidget(btn)
            self.devices_box.addWidget(row)

    def _on_tick(self) -> None:
        st = self.rt.pairing.state()
        if st.active and st.code:
            left = max(0.0, st.expires_at - time.monotonic())
            self.code_bar.setMaximum(int(self.rt.pairing.ttl_s))
            self.code_bar.setValue(int(left))
            self.code_info.setText(t("code_valid", t=f"{int(left) // 60}:{int(left) % 60:02d}"))
        elif self.code_box.isVisible():
            self.code_lbl.setText("––– –––")
            self.code_info.setText(t("code_expired"))
            self.code_cancel.setText(t("new_code"))
            self.code_bar.setValue(0)

    # ---- actions ------------------------------------------------------------------------------------
    def _start_pairing(self) -> None:
        self.code_cancel.setText(t("cancel"))
        self.rt.start_pairing()

    def _cancel_pairing(self) -> None:
        if self.rt.pairing.state().active:
            self.rt.cancel_pairing()
        else:
            self._start_pairing()  # "new code" after expiry

    def _unpair(self, client_id: str, name: str) -> None:
        if QMessageBox.question(self, t("app"), t("unpair_confirm", name=name)) == QMessageBox.StandardButton.Yes:
            self.rt.revoke(client_id)
            self._refresh_devices()

    def _toggle_autostart(self, on: bool) -> None:
        ok, msg = autostart.set_enabled(on)
        if not ok:
            QMessageBox.warning(self, t("app"), t("autostart_fail", msg=msg))
            self.autostart.blockSignals(True)
            self.autostart.setChecked(autostart.is_enabled())
            self.autostart.blockSignals(False)

    def _quit(self) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.instance().quit()

    # ---- window behaviour -------------------------------------------------------------------------------
    hidden_to_tray = Signal()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt API)
        event.ignore()
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.showMinimized()  # e.g. GNOME without an AppIndicator extension: never hide for good
            return
        self.hide()
        if not self._hidden_hint_shown:
            self._hidden_hint_shown = True
            self.hidden_to_tray.emit()

    def bring_to_front(self) -> None:
        self.show()
        self.setWindowState((self.windowState() & ~Qt.WindowState.WindowMinimized) | Qt.WindowState.WindowActive)
        self.raise_()
        self.activateWindow()
