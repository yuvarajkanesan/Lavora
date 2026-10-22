"""Vector tile icons drawn with QPainter (no image files, crisp at any DPI, follow the theme's accent colour)."""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

SIZE = 96  # drawn large, scaled down by Qt


def _canvas(accent: str) -> tuple[QPixmap, QPainter]:
    pm = QPixmap(SIZE, SIZE)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    bg = QColor(accent)
    bg.setAlpha(34)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(bg)
    p.drawRoundedRect(QRectF(0, 0, SIZE, SIZE), 22, 22)
    pen = QPen(QColor(accent), 6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    return pm, p


def _power(p: QPainter) -> None:
    p.drawArc(QRectF(24, 26, 48, 48), 120 * 16, -300 * 16)
    p.drawLine(QPointF(48, 18), QPointF(48, 46))


def _recover(p: QPainter) -> None:  # document with an upward arrow: "bring it back"
    path = QPainterPath()
    path.moveTo(30, 66)
    path.lineTo(30, 24)
    path.lineTo(54, 24)
    path.lineTo(66, 36)
    path.lineTo(66, 66)
    path.closeSubpath()
    p.drawPath(path)
    p.drawPolyline([QPointF(54, 24), QPointF(54, 36), QPointF(66, 36)])
    p.drawLine(QPointF(48, 60), QPointF(48, 44))
    p.drawPolyline([QPointF(41, 50), QPointF(48, 43), QPointF(55, 50)])


def _key(p: QPainter) -> None:
    p.drawEllipse(QRectF(22, 32, 24, 24))
    p.drawLine(QPointF(46, 44), QPointF(74, 44))
    p.drawLine(QPointF(66, 44), QPointF(66, 54))
    p.drawLine(QPointF(74, 44), QPointF(74, 52))


def _disk_warning(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(20, 30, 56, 38), 8, 8)
    p.drawLine(QPointF(48, 40), QPointF(48, 52))
    p.drawPoint(QPointF(48, 60))
    p.drawLine(QPointF(28, 60), QPointF(30, 60))


def _shield(p: QPainter) -> None:
    path = QPainterPath()
    path.moveTo(48, 20)
    path.lineTo(70, 29)
    path.lineTo(70, 46)
    path.cubicTo(70, 60, 60, 69, 48, 74)
    path.cubicTo(36, 69, 26, 60, 26, 46)
    path.lineTo(26, 29)
    path.closeSubpath()
    p.drawPath(path)
    p.drawPolyline([QPointF(38, 47), QPointF(46, 55), QPointF(60, 38)])


def _terminal(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(18, 26, 60, 44), 8, 8)
    p.drawPolyline([QPointF(30, 40), QPointF(40, 48), QPointF(30, 56)])
    p.drawLine(QPointF(46, 58), QPointF(62, 58))


def _raid(p: QPainter) -> None:  # three stacked disks
    for y in (24, 42, 60):
        p.drawRoundedRect(QRectF(22, y, 52, 14), 6, 6)
        p.drawPoint(QPointF(64, y + 7))


def _editor(p: QPainter) -> None:  # a grid of hex cells
    for r in range(3):
        for c in range(3):
            p.drawRoundedRect(QRectF(24 + c * 18, 26 + r * 16, 12, 10), 3, 3)


GLYPHS = {"raid": _raid, "editor": _editor, "boot": _power, "recover": _recover, "password": _key, "image": _disk_warning, "backup": _shield, "plans": _terminal}


def tile_icon(name: str, accent: str) -> QIcon:
    pm, p = _canvas(accent)
    GLYPHS[name](p)
    p.end()
    return QIcon(pm)
