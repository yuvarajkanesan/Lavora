"""Vector icons drawn with QPainter (no image files: crisp at any DPI, recoloured per theme)."""
from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

SIZE = 96  # glyphs are designed on a 96x96 grid and scaled


def _power(p: QPainter) -> None:
    p.drawArc(QRectF(24, 26, 48, 48), 120 * 16, -300 * 16)
    p.drawLine(QPointF(48, 18), QPointF(48, 46))


def _recover(p: QPainter) -> None:  # document with an upward arrow
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


def _file(p: QPainter) -> None:  # plain document
    path = QPainterPath()
    path.moveTo(30, 70)
    path.lineTo(30, 22)
    path.lineTo(56, 22)
    path.lineTo(68, 34)
    path.lineTo(68, 70)
    path.closeSubpath()
    p.drawPath(path)
    p.drawPolyline([QPointF(56, 22), QPointF(56, 34), QPointF(68, 34)])


def _key(p: QPainter) -> None:
    p.drawEllipse(QRectF(22, 40, 22, 22))
    p.drawLine(QPointF(40, 44), QPointF(72, 20))
    p.drawLine(QPointF(62, 28), QPointF(68, 36))
    p.drawLine(QPointF(70, 22), QPointF(76, 30))


def _disk_alert(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(18, 26, 60, 44), 10, 10)
    p.drawLine(QPointF(48, 36), QPointF(48, 50))
    p.drawPoint(QPointF(48, 60))


def _shield(p: QPainter) -> None:
    path = QPainterPath()
    path.moveTo(48, 18)
    path.lineTo(72, 28)
    path.lineTo(72, 46)
    path.cubicTo(72, 61, 61, 71, 48, 77)
    path.cubicTo(35, 71, 24, 61, 24, 46)
    path.lineTo(24, 28)
    path.closeSubpath()
    p.drawPath(path)
    p.drawPolyline([QPointF(37, 47), QPointF(45, 55), QPointF(60, 37)])


def _terminal(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(16, 24, 64, 48), 9, 9)
    p.drawPolyline([QPointF(30, 40), QPointF(41, 49), QPointF(30, 58)])
    p.drawLine(QPointF(48, 60), QPointF(64, 60))


def _raid(p: QPainter) -> None:  # two stacked drives
    for y in (22, 52):
        p.drawRoundedRect(QRectF(20, y, 56, 22), 8, 8)
        p.drawPoint(QPointF(30, y + 11))
        p.drawPoint(QPointF(40, y + 11))


def _editor(p: QPainter) -> None:  # grid of hex cells
    for r in range(3):
        for c in range(3):
            p.drawRoundedRect(QRectF(22 + c * 20, 24 + r * 17, 14, 11), 3, 3)


def _search(p: QPainter) -> None:
    p.drawEllipse(QRectF(22, 22, 40, 40))
    p.drawLine(QPointF(56, 56), QPointF(74, 74))


def _lock(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(26, 42, 44, 32), 8, 8)
    path = QPainterPath()
    path.moveTo(35, 42)
    path.lineTo(35, 34)
    path.arcTo(QRectF(35, 20, 26, 28), 180, -180)
    path.lineTo(61, 42)
    p.drawPath(path)


def _eye_off(p: QPainter) -> None:
    path = QPainterPath()
    path.moveTo(14, 48)
    path.cubicTo(28, 28, 68, 28, 82, 48)
    path.cubicTo(68, 68, 28, 68, 14, 48)
    p.drawPath(path)
    p.drawEllipse(QRectF(40, 40, 16, 16))
    p.drawLine(QPointF(22, 76), QPointF(76, 20))


def _chevron_r(p: QPainter) -> None:
    p.drawPolyline([QPointF(36, 24), QPointF(60, 48), QPointF(36, 72)])


def _chevron_l(p: QPainter) -> None:
    p.drawPolyline([QPointF(60, 24), QPointF(36, 48), QPointF(60, 72)])


def _chevron_d(p: QPainter) -> None:
    p.drawPolyline([QPointF(24, 36), QPointF(48, 62), QPointF(72, 36)])


def _auto(p: QPainter) -> None:  # circular arrow: "do it all"
    p.drawArc(QRectF(22, 22, 52, 52), 40 * 16, 280 * 16)
    p.drawPolyline([QPointF(62, 14), QPointF(72, 28), QPointF(56, 32)])
    p.drawLine(QPointF(48, 38), QPointF(48, 52))
    p.drawPolyline([QPointF(41, 46), QPointF(48, 53), QPointF(55, 46)])


def _pulse(p: QPainter) -> None:  # heartbeat line
    p.drawPolyline([QPointF(14, 50), QPointF(32, 50), QPointF(40, 28), QPointF(52, 70), QPointF(60, 50), QPointF(82, 50)])


def _check(p: QPainter) -> None:
    p.drawPolyline([QPointF(22, 50), QPointF(40, 68), QPointF(74, 30)])


def _check_circle(p: QPainter) -> None:
    p.drawEllipse(QRectF(14, 14, 68, 68))
    p.drawPolyline([QPointF(30, 49), QPointF(43, 62), QPointF(66, 36)])


def _info_circle(p: QPainter) -> None:
    p.drawEllipse(QRectF(14, 14, 68, 68))
    p.drawLine(QPointF(48, 44), QPointF(48, 66))
    p.drawPoint(QPointF(48, 32))


def _drive(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(16, 34, 64, 28), 9, 9)
    p.drawPoint(QPointF(66, 48))


GLYPHS: dict[str, Callable[[QPainter], None]] = {
    "boot": _power, "recover": _recover, "password": _key, "image": _disk_alert, "backup": _shield, "plans": _terminal,
    "raid": _raid, "editor": _editor, "file": _file, "search": _search, "lock": _lock, "eye-off": _eye_off,
    "auto": _auto, "health": _pulse, "chevron-right": _chevron_r, "chevron-down": _chevron_d, "chevron-left": _chevron_l, "check": _check, "check-circle": _check_circle,
    "info-circle": _info_circle, "drive": _drive, "shield": _shield,
}


def _finish(pm: QPixmap, px: int) -> QPixmap:
    out = pm.scaled(px * 2, px * 2, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    out.setDevicePixelRatio(2)  # drawn at 2x, so it stays sharp and still lays out as px x px
    return out


def _pixmap(name: str, color: str, px: int, stroke: float, bg: QColor | None = None) -> QPixmap:
    scale = 4
    big = px * scale
    pm = QPixmap(big, big)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    if bg is not None:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(QRectF(0, 0, big, big), big * 0.24, big * 0.24)
    p.scale(big / SIZE, big / SIZE)
    p.setPen(QPen(QColor(color), stroke, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.setBrush(Qt.BrushStyle.NoBrush)
    GLYPHS[name](p)
    p.end()
    return _finish(pm, px)


def glyph_icon(name: str, color: str, px: int = 24, stroke: float = 7) -> QIcon:
    return QIcon(_pixmap(name, color, px, stroke))


def glyph_pixmap(name: str, color: str, px: int = 24, stroke: float = 7) -> QPixmap:
    return _pixmap(name, color, px, stroke)


def tile_pixmap(name: str, accent: str, px: int = 56, *, on_dark: bool = False, soft: str | None = None) -> QPixmap:
    """The rounded icon tile used on cards: soft accent background with an accent glyph (white glyph on a dark card)."""
    if on_dark:
        return _pixmap(name, "#ffffff", px, 6.5, QColor(255, 255, 255, 56))
    bg = QColor(soft or accent)
    if soft is None:
        bg.setAlpha(34)
    return _pixmap(name, accent, px, 6.5, bg)


def tile_icon(name: str, accent: str) -> QIcon:
    return QIcon(tile_pixmap(name, accent, 96))


def app_logo(px: int = 44, accent: str = "#5b3fd1") -> QPixmap:
    """Rounded accent square with a white shield and tick (the same mark as the installer icon)."""
    scale = 4
    big = px * scale
    pm = QPixmap(big, big)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(accent))
    p.drawRoundedRect(QRectF(0, 0, big, big), big * 0.26, big * 0.26)
    p.scale(big / SIZE, big / SIZE)
    p.setPen(QPen(QColor("white"), 7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.setBrush(Qt.BrushStyle.NoBrush)
    _shield(p)
    p.end()
    return _finish(pm, px)
