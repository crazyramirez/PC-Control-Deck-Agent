"""App / tray icons drawn with QPainter (no image assets to ship or license)."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

BG = QColor("#171F2D")
BORDER = QColor("#304056")
CYAN = QColor("#52D5E8")
STATUS_COLORS = {
    "ok": QColor("#68E5A0"),
    "idle": QColor("#A8B5C9"),
    "pairing": QColor("#F4C66F"),
    "error": QColor("#F87171"),
}


def _draw(size: int, status: str | None) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = float(size)
    # rounded "device" body
    body = QRectF(s * 0.06, s * 0.06, s * 0.88, s * 0.88)
    p.setPen(QPen(BORDER, max(1.0, s * 0.03)))
    p.setBrush(BG)
    p.drawRoundedRect(body, s * 0.2, s * 0.2)
    # gauge arc
    pen = QPen(CYAN, max(1.5, s * 0.09))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    arc = QRectF(s * 0.24, s * 0.26, s * 0.52, s * 0.52)
    p.drawArc(arc, 210 * 16, -240 * 16)
    # needle
    needle = QPainterPath()
    needle.moveTo(QPointF(s * 0.5, s * 0.52))
    needle.lineTo(QPointF(s * 0.66, s * 0.38))
    p.drawPath(needle)
    if status:
        c = STATUS_COLORS.get(status, STATUS_COLORS["idle"])
        r = s * 0.17
        p.setPen(QPen(BG, max(1.0, s * 0.05)))
        p.setBrush(c)
        p.drawEllipse(QPointF(s * 0.78, s * 0.78), r, r)
    p.end()
    return pm


def app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 20, 24, 32, 40, 48, 64, 128, 256):
        icon.addPixmap(_draw(size, None))
    return icon


def tray_icon(status: str) -> QIcon:
    icon = QIcon()
    for size in (16, 20, 24, 32, 40, 48, 64):
        icon.addPixmap(_draw(size, status))
    return icon


def save_ico(path: str) -> bool:
    """Write a 256 px .ico for PyInstaller / the installer (Qt's ICO writer)."""
    return _draw(256, None).save(path, "ICO")
