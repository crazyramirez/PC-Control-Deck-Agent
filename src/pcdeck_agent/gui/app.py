"""PC Control Deck tray application (entry point of the installed .exe).

  PCControlDeck.exe              start (or bring the running instance to front) and show the window
  PCControlDeck.exe --minimized  start in the tray only (used by the start-with-Windows task)
  PCControlDeck.exe --fake       synthetic sensors (demo / development)
"""

from __future__ import annotations

import argparse
import logging
import sys

from PySide6.QtCore import QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from .. import __version__
from ..config import app_dir, load_config
from ..logging_setup import setup_logging
from ..runtime import AgentRuntime
from . import autostart
from .icons import app_icon, tray_icon
from .strings import t
from .window import Bridge, MainWindow

log = logging.getLogger("pcdeck_agent.gui")
SINGLE_INSTANCE_KEY = "PcControlDeck.SingleInstance"


def _provider_factory(fake: bool):
    def make():
        if fake:
            from ..hardware.demo_frames import demo_provider

            return demo_provider()
        from ..hardware.lhm_provider import LhmProvider

        return LhmProvider()

    return make


def _notify_running_instance() -> bool:
    """True if another instance is running (it is asked to show its window)."""
    sock = QLocalSocket()
    sock.connectToServer(SINGLE_INSTANCE_KEY)
    if sock.waitForConnected(300):
        sock.write(b"show")
        sock.flush()
        sock.waitForBytesWritten(300)
        sock.disconnectFromServer()
        return True
    return False


def _relaunch_elevated_via_task(args: argparse.Namespace) -> bool:
    """Installed build started without elevation (Start menu, Explorer): start the elevated
    on-demand task the installer registered instead, so sensors work with no UAC prompt."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False) or args.screenshot:
        return False
    import ctypes
    import subprocess

    if ctypes.windll.shell32.IsUserAnAdmin():
        return False
    if not autostart.task_exists(autostart.TASK_RUN):
        return False
    r = subprocess.run(["schtasks", "/Run", "/TN", autostart.TASK_RUN], capture_output=True, creationflags=0x08000000, timeout=15)
    return r.returncode == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--minimized", action="store_true")
    parser.add_argument("--fake", action="store_true")
    parser.add_argument("--screenshot", help="dev aid: save the window to this PNG after a few seconds and quit")
    parser.add_argument("--with-code", action="store_true", help="dev aid: show a pairing code before the screenshot")
    args, _ = parser.parse_known_args(argv)

    app = QApplication(sys.argv[:1])
    app.setApplicationName("PC Control Deck")
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(app_icon())

    server = QLocalServer()
    if not args.screenshot:  # dev captures run side by side with an installed copy
        if _notify_running_instance():
            return 0
        if _relaunch_elevated_via_task(args):
            return 0
        QLocalServer.removeServer(SINGLE_INSTANCE_KEY)
        server.listen(SINGLE_INSTANCE_KEY)

    cfg, cfg_path = load_config()
    if args.screenshot:
        cfg.port, cfg.mdns = 0, False  # ephemeral port, no advertisement
    log_path = setup_logging(cfg.log_level, app_dir() / "logs")
    log.info("PC Control Deck %s (tray), config %s, log %s", __version__, cfg_path, log_path)

    rt = AgentRuntime(cfg, _provider_factory(args.fake))
    bridge = Bridge()
    rt.on_status = bridge.status.emit
    rt.on_sessions = bridge.sessions.emit
    rt.on_paired = bridge.paired.emit
    rt.on_pairing = bridge.pairing.emit

    win = MainWindow(rt, bridge)

    tray = QSystemTrayIcon(tray_icon("idle"))
    tray.setToolTip(f"{t('app')} {__version__}")
    menu = QMenu()
    act_open = menu.addAction(t("open"))
    act_open.triggered.connect(win.bring_to_front)
    act_pair = menu.addAction(t("pair_button"))
    act_pair.triggered.connect(lambda: (win.bring_to_front(), win._start_pairing()))
    menu.addSeparator()
    act_auto = menu.addAction(t("autostart"))
    act_auto.setCheckable(True)
    act_auto.setChecked(autostart.is_enabled())
    act_auto.toggled.connect(win.autostart.setChecked)
    win.autostart.toggled.connect(act_auto.setChecked)
    menu.addSeparator()
    act_quit = menu.addAction(t("quit"))
    act_quit.triggered.connect(app.quit)
    tray.setContextMenu(menu)
    tray.activated.connect(lambda reason: win.bring_to_front() if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick) else None)
    tray.show()

    def on_state(state: str) -> None:
        tray.setIcon(tray_icon(state))
        tray.setToolTip(f"{t('app')} {__version__} · {win.subtitle.text()}")

    win.state_changed.connect(on_state)
    win.hidden_to_tray.connect(lambda: tray.showMessage(t("app"), t("tray_hidden"), tray_icon("idle"), 4000))
    bridge.paired.connect(lambda cid, name: tray.showMessage(t("app"), t("paired_ok", name=name), tray_icon("ok"), 5000))

    def on_new_instance() -> None:
        conn = server.nextPendingConnection()
        if conn:
            conn.readyRead.connect(lambda: (conn.readAll(), win.bring_to_front()))

    server.newConnection.connect(on_new_instance)

    rt.start()
    if not args.minimized:
        win.bring_to_front()

    def shutdown() -> None:
        log.info("shutting down")
        rt.stop()
        tray.hide()

    app.aboutToQuit.connect(shutdown)
    if args.screenshot:
        if args.with_code:
            QTimer.singleShot(2500, win._start_pairing)
        QTimer.singleShot(5000, lambda: (win.grab().save(args.screenshot), app.quit()))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
