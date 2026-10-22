"""Reusable UI pieces: top bar, pills, cards, stepper, choice cards, chance bars, banners."""
from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from ..brand import NAME
from . import icons, theme

CHANCE_ROLE = Qt.ItemDataRole.UserRole + 1  # (percent 0..100, label, level) on the "chance" column items


def palette(name: str) -> dict[str, str]:
    return theme.PALETTES[name]


def repolish(w: QWidget) -> None:
    w.style().unpolish(w)
    w.style().polish(w)


class IconLabel(QLabel):
    def __init__(self, name: str, color: str, px: int = 20, stroke: float = 7):
        super().__init__()
        self.setPixmap(icons.glyph_pixmap(name, color, px, stroke))
        self.setFixedSize(px, px)
        self.setScaledContents(False)


class Pill(QFrame):
    """Rounded status pill, optionally with an icon (e.g. the lock in 'Read-only mode')."""

    def __init__(self, text: str, tone: str, theme_name: str, icon: str | None = None):
        super().__init__()
        c = palette(theme_name)
        fg, bg = (c["ok"], c["ok_soft"]) if tone == "ok" else (c["accent"], c["accent_soft"])
        self.setFixedHeight(36)
        self.setStyleSheet(f"QFrame {{ background: {bg}; border-radius: 18px; }} QLabel {{ background: transparent; color: {fg}; font-weight: 600; font-size: 14px; }}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 0, 18, 0)
        lay.setSpacing(8)
        if icon:
            lay.addWidget(IconLabel(icon, fg, 16, 8))
        self.label = QLabel(text)
        lay.addWidget(self.label)


class TopBar(QFrame):
    def __init__(self, version: str, pro: bool, theme_name: str, tier_text: str, readonly_text: str):
        super().__init__()
        self.setObjectName("topbar")
        self.setFixedHeight(72)
        c = palette(theme_name)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(28, 0, 28, 0)
        lay.setSpacing(14)
        logo = QLabel()
        logo.setPixmap(icons.app_logo(44, c["accent"]))
        logo.setFixedSize(44, 44)
        name = QLabel(NAME)
        name.setObjectName("brand")
        ver = QLabel(version)
        ver.setObjectName("version")
        lay.addWidget(logo)
        lay.addWidget(name)
        ver.setStyleSheet("padding-top: 4px;")
        lay.addWidget(ver, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addStretch(1)
        self.tier = Pill(tier_text, "accent", theme_name)
        lay.addWidget(self.tier, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(Pill(readonly_text, "ok", theme_name, "lock"), 0, Qt.AlignmentFlag.AlignVCenter)


class Card(QFrame):
    def __init__(self, margins: tuple[int, int, int, int] = (28, 26, 28, 26), spacing: int = 14):
        super().__init__()
        self.setObjectName("card")
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(*margins)
        self.lay.setSpacing(spacing)


class ActionCard(QFrame):
    """A clickable home-screen card: icon tile, title + subtitle, chevron. `primary` fills it with the accent colour."""

    clicked = Signal()

    def __init__(self, icon: str, title: str, subtitle: str, theme_name: str, *, primary: bool = False):
        super().__init__()
        c = palette(theme_name)
        self.setObjectName("actionCard")
        self.setProperty("primary", primary)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(118)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(26, 22, 24, 22)
        lay.setSpacing(18)
        tile = QLabel()
        tile.setPixmap(icons.tile_pixmap(icon, c["accent"], 58, on_dark=primary, soft=c["accent_soft"]))
        tile.setFixedSize(58, 58)
        text = QVBoxLayout()
        text.setSpacing(3)
        t = QLabel(title)
        t.setObjectName("cardTitle")
        s = QLabel(subtitle)
        s.setObjectName("cardSub")
        s.setWordWrap(True)
        text.addStretch(1)
        text.addWidget(t)
        text.addWidget(s)
        text.addStretch(1)
        chev = IconLabel("chevron-right", "#ffffff" if primary else c["muted"], 22, 8)
        lay.addWidget(tile)
        lay.addLayout(text, 1)
        lay.addWidget(chev)

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802 - Qt override
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(e)


class FeatureChip(QFrame):
    """The small outlined 'Read-only by default' style chips under the home heading."""

    def __init__(self, icon: str, text: str, theme_name: str):
        super().__init__()
        c = palette(theme_name)
        self.setObjectName("chip")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 9, 20, 9)
        lay.setSpacing(10)
        lay.addWidget(IconLabel(icon, c["ok"], 18, 8))
        lay.addWidget(QLabel(text))


class ChoiceCard(QFrame):
    """Radio-style option card (e.g. 'Scan for deleted files' / 'Deep scan')."""

    selected_changed = Signal(bool)

    def __init__(self, title: str, desc: str, theme_name: str):
        super().__init__()
        self._c = palette(theme_name)
        self.setObjectName("choice")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._selected = False
        self.group: list[ChoiceCard] = []
        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(14)
        self.dot = _RadioDot(self._c["accent"], self._c["border_strong"])
        text = QVBoxLayout()
        text.setSpacing(4)
        t = QLabel(title)
        t.setObjectName("choiceTitle")
        d = QLabel(desc)
        d.setObjectName("choiceSub")
        d.setWordWrap(True)
        text.addWidget(t)
        text.addWidget(d)
        lay.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(text, 1)
        self.setProperty("selected", False)

    @property
    def selected(self) -> bool:
        return self._selected

    def set_selected(self, on: bool) -> None:
        self._selected = on
        self.dot.on = on
        self.dot.update()
        self.setProperty("selected", on)
        repolish(self)

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.MouseButton.LeftButton:
            for other in self.group:
                other.set_selected(other is self)
            self.selected_changed.emit(True)
        super().mouseReleaseEvent(e)


class _RadioDot(QWidget):
    def __init__(self, on_color: str, off_color: str):
        super().__init__()
        self.on, self._on_c, self._off_c = False, QColor(on_color), QColor(off_color)
        self.setFixedSize(22, 22)

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(self._on_c if self.on else self._off_c, 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QRectF(3, 3, 16, 16))
        if self.on:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self._on_c)
            p.drawEllipse(QRectF(6.5, 6.5, 9, 9))


class Stepper(QWidget):
    """1 Choose source  2 Scan  3 Review and preview  4 Recover. Done steps show a green tick, the current one is filled."""

    def __init__(self, labels: list[str], theme_name: str):
        super().__init__()
        self.labels, self.c, self.current = labels, palette(theme_name), 1
        self.setFixedHeight(52)
        self.setMinimumWidth(640)

    def set_step(self, n: int) -> None:
        self.current = n
        self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont(self.font())
        font.setPixelSize(17)
        x = 0.0
        for i, label in enumerate(self.labels, start=1):
            done, active = i < self.current, i == self.current
            circle = QRectF(x + 1, 6, 40, 40)
            if done:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(self.c["ok"]))
            elif active:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(self.c["accent"]))
            else:
                p.setPen(QPen(QColor(self.c["border_strong"]), 1.5))
                p.setBrush(QColor(self.c["panel"]))
            p.drawEllipse(circle)
            p.setPen(QColor("white") if (done or active) else QColor(self.c["muted"]))
            if done:
                p.setPen(QPen(QColor("white"), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
                cx, cy = circle.center().x(), circle.center().y()
                p.drawPolyline([QPointF_(cx - 8, cy + 1), QPointF_(cx - 2, cy + 7), QPointF_(cx + 9, cy - 6)])
            else:
                f2 = QFont(font)
                f2.setBold(True)
                p.setFont(f2)
                p.drawText(circle, Qt.AlignmentFlag.AlignCenter, str(i))
            f3 = QFont(font)
            f3.setBold(active)
            f3.setWeight(QFont.Weight.Bold if active else QFont.Weight.Normal)
            p.setFont(f3)
            p.setPen(QColor(self.c["text"] if active else self.c["muted"]))
            tw = p.fontMetrics().horizontalAdvance(label)
            p.drawText(QRectF(x + 54, 0, tw + 4, 52), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, label)
            x += 54 + tw + 44

    def sizeHint(self) -> QSize:
        return QSize(820, 52)


def QPointF_(x: float, y: float):  # noqa: N802 - tiny helper to keep the paint code readable
    from PySide6.QtCore import QPointF
    return QPointF(x, y)


class ChanceDelegate(QStyledItemDelegate):
    """Draws the coloured recovery-chance bar plus 'High · 96%' text in the table's chance column."""

    def __init__(self, theme_name: str):
        super().__init__()
        self.c = palette(theme_name)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        data = index.data(CHANCE_ROLE)
        if not data:
            super().paint(painter, option, index)
            return
        pct, label, level = data
        # background (selection / hover) first
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        opt.widget.style().drawControl(opt.widget.style().ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        color = QColor({"high": self.c["ok"], "medium": self.c["warn"], "low": self.c["danger"]}.get(level, self.c["faint"]))
        r = option.rect
        bar = QRectF(r.x() + 8, r.center().y() - 4, 78, 8)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(self.c["track"]))
        painter.drawRoundedRect(bar, 4, 4)
        if pct > 0:
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(bar.x(), bar.y(), max(8.0, bar.width() * pct / 100), bar.height()), 4, 4)
        painter.setPen(QColor(self.c["text"]))
        f = QFont(option.font)
        f.setPixelSize(14)
        painter.setFont(f)
        painter.drawText(QRectF(bar.right() + 10, r.y(), r.width() - 100, r.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, label)
        painter.restore()

    def sizeHint(self, option, index) -> QSize:
        s = super().sizeHint(option, index)
        return QSize(max(s.width(), 190), max(s.height(), 52))


class Banner(QFrame):
    """Status strip at the top of a results card: success / info / warn / error, optional progress + cancel."""

    def __init__(self, theme_name: str):
        super().__init__()
        self.setObjectName("banner")
        self._tn = theme_name
        lay = QHBoxLayout(self)
        lay.setContentsMargins(26, 16, 22, 16)
        lay.setSpacing(12)
        self.icon = QLabel()
        self.icon.setFixedSize(24, 24)
        self.text = QLabel("")
        self.bar = QProgressBar()
        self.bar.setFixedWidth(220)
        self.bar.setMaximum(1000)
        self.bar.setVisible(False)
        self.cancel = QPushButton("Cancel")
        self.cancel.setObjectName("link")
        self.cancel.setVisible(False)
        lay.addWidget(self.icon)
        lay.addWidget(self.text, 1)
        lay.addWidget(self.bar)
        lay.addWidget(self.cancel)
        self.setVisible(False)

    def show_message(self, kind: str, text: str, *, progress: bool = False, cancellable: bool = False) -> None:
        c = palette(self._tn)
        color = {"success": c["ok"], "info": c["accent"], "warn": c["warn"], "error": c["danger"]}[kind]
        name = {"success": "check-circle", "info": "info-circle", "warn": "info-circle", "error": "info-circle"}[kind]
        self.icon.setPixmap(icons.glyph_pixmap(name, color, 24, 7))
        self.text.setText(text)
        self.setProperty("kind", kind)
        repolish(self)
        for child in self.findChildren(QLabel):
            repolish(child)
        self.bar.setVisible(progress)
        self.cancel.setVisible(cancellable)
        self.setVisible(True)
