"""More building blocks: tags, notices, code box, numbered/check lists, and radio / badge table-cell painters."""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from .widgets import IconLabel, palette

BADGE_ROLE = Qt.ItemDataRole.UserRole + 2  # (text, tone) on cells painted by BadgeDelegate


def tag_style(tone: str, c: dict[str, str]) -> str:
    fg, bg = {"ok": (c["ok"], c["ok_soft"]), "warn": (c["warn"], c["warn_soft"]), "danger": (c["danger"], c["danger_soft"]),
              "accent": (c["accent"], c["accent_soft"]), "neutral": (c["muted"], c["track"])}[tone]
    return f"background: {bg}; color: {fg}; border-radius: 14px; padding: 6px 14px; font-size: 13.5px; font-weight: 700;"


class Tag(QLabel):
    """Small rounded status label: 'Not encrypted', 'Available', 'Not in this version', 'Not started'."""

    def __init__(self, text: str, tone: str, theme_name: str):
        super().__init__(text)
        self._c = palette(theme_name)
        self.set_tone(tone)

    def set_tone(self, tone: str, text: str | None = None) -> None:
        if text is not None:
            self.setText(text)
        self.setStyleSheet(tag_style(tone, self._c))
        self.adjustSize()


class Notice(QFrame):
    """Soft coloured strip with an icon, e.g. the amber 'destination must be on a different drive' warning."""

    def __init__(self, text: str, tone: str, icon: str, theme_name: str):
        super().__init__()
        c = palette(theme_name)
        fg, bg = {"ok": (c["ok"], c["ok_soft"]), "warn": (c["warn"], c["warn_soft"]), "info": (c["accent"], c["accent_soft"])}[tone]
        self.setStyleSheet(f"QFrame {{ background: {bg}; border-radius: 12px; }} QLabel {{ background: transparent; color: {fg}; font-size: 15px; }}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(12)
        lay.addWidget(IconLabel(icon, fg, 20, 7), 0, Qt.AlignmentFlag.AlignTop)
        self.label = QLabel(text)
        self.label.setWordWrap(True)
        lay.addWidget(self.label, 1)


class CodeBlock(QFrame):
    """Dark monospace box for commands and the activity log."""

    def __init__(self, text: str = "", *, min_height: int = 0):
        super().__init__()
        self.setStyleSheet("QFrame { background: #1e1b33; border-radius: 14px; } QLabel { background: transparent; color: #f1effa; "
                           "font-family: 'JetBrains Mono', 'Cascadia Mono', Consolas, monospace; font-size: 14.5px; }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        self.label = QLabel(text)
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(self.label)
        lay.addStretch(1)
        if min_height:
            self.setMinimumHeight(min_height)

    def set_text(self, text: str) -> None:
        self.label.setText(text)

    def text(self) -> str:
        return self.label.text()


class NumberedList(QWidget):
    """Numbered explanation list: a round number badge and rich text ('<b>Incremental.</b> After the first run...')."""

    linkActivated = Signal(str)  # noqa: N815 - Qt naming

    def __init__(self, items: list[str], theme_name: str, *, size: int = 15):
        super().__init__()
        c = palette(theme_name)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)
        for i, html in enumerate(items, start=1):
            row = QHBoxLayout()
            row.setSpacing(14)
            n = QLabel(str(i))
            n.setFixedSize(34, 34)
            n.setAlignment(Qt.AlignmentFlag.AlignCenter)
            n.setStyleSheet(f"background: {c['accent_soft']}; color: {c['accent']}; border-radius: 17px; font-weight: 700; font-size: 14px;")
            t = QLabel(html)
            t.setWordWrap(True)
            t.setTextFormat(Qt.TextFormat.RichText)
            t.setStyleSheet(f"font-size: {size}px;")
            t.linkActivated.connect(self.linkActivated)
            row.addWidget(n, 0, Qt.AlignmentFlag.AlignTop)
            row.addWidget(t, 1)
            lay.addLayout(row)


class CheckList(QWidget):
    """Green tick + sentence rows ('Before you start')."""

    def __init__(self, items: list[str], theme_name: str):
        super().__init__()
        c = palette(theme_name)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)
        for text in items:
            row = QHBoxLayout()
            row.setSpacing(14)
            row.addWidget(IconLabel("check", c["ok"], 22, 9), 0, Qt.AlignmentFlag.AlignTop)
            t = QLabel(text)
            t.setStyleSheet("font-size: 17px;")
            t.setWordWrap(True)
            row.addWidget(t, 1)
            lay.addLayout(row)


def _draw_base(opt: QStyleOptionViewItem, painter: QPainter, text_hidden: bool) -> None:
    if text_hidden:
        opt.text = ""
    style = opt.widget.style()
    style.drawControl(style.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)


class RadioDelegate(QStyledItemDelegate):
    """Radio dot in column 0: filled when the row is the current selection."""

    def __init__(self, theme_name: str):
        super().__init__()
        self.c = palette(theme_name)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        _draw_base(opt, painter, True)
        view = option.widget
        sel = view.selectionModel() if isinstance(view, QAbstractItemView) else None
        selected = sel is not None and sel.isRowSelected(index.row(), index.parent())
        cx, cy = option.rect.center().x(), option.rect.center().y()
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(self.c["accent"] if selected else self.c["border_strong"]), 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QRectF(cx - 10, cy - 10, 20, 20))
        if selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(self.c["accent"]))
            painter.drawEllipse(QRectF(cx - 5.5, cy - 5.5, 11, 11))
        painter.restore()


class BadgeDelegate(QStyledItemDelegate):
    """Coloured pill (Critical / Warning / Info) from BADGE_ROLE data."""

    def __init__(self, theme_name: str):
        super().__init__()
        self.c = palette(theme_name)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        data = index.data(BADGE_ROLE)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        _draw_base(opt, painter, bool(data))
        if not data:
            return
        text, tone = data
        fg, bg = {"danger": (self.c["danger"], self.c["danger_soft"]), "warn": (self.c["warn"], self.c["warn_soft"]),
                  "info": (self.c["accent"], self.c["accent_soft"]), "ok": (self.c["ok"], self.c["ok_soft"])}.get(tone, (self.c["muted"], self.c["track"]))
        f = QFont(option.font)
        f.setPixelSize(14)
        f.setBold(True)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setFont(f)
        tw = painter.fontMetrics().horizontalAdvance(text)
        r = option.rect
        pill = QRectF(r.x() + 8, r.center().y() - 15, tw + 32, 30)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(bg))
        painter.drawRoundedRect(pill, 15, 15)
        painter.setPen(QColor(fg))
        painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()
