"""Shared page scaffolding: background workers, scrollable centred page body, breadcrumb header, source picker."""
from __future__ import annotations

import traceback
from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..brand import NAME
from ..core import disks
from ..core.cancel import CancelToken
from ..i18n import tr
from . import icons
from .widgets import Card, palette

if TYPE_CHECKING:
    from .app import MainWindow

CONTENT_MAX_WIDTH = 1760


class Worker(QObject):
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int)

    def __init__(self, fn: Callable[[Callable[[int, int], None]], object]):
        super().__init__()
        self.fn = fn

    def run(self) -> None:
        try:
            self.done.emit(self.fn(lambda d, t: self.progress.emit(d, t)))
        except Exception as ex:  # noqa: BLE001 - shown to the user, never swallowed silently
            traceback.print_exc()
            self.failed.emit(f"{type(ex).__name__}: {ex}")


class Page(QWidget):
    """A scrollable page whose content is centred and capped at CONTENT_MAX_WIDTH."""

    def __init__(self, win: MainWindow):
        super().__init__()
        self.win = win
        self.theme = win.theme_name
        self._threads: list[QThread] = []
        self._workers: list[Worker] = []
        self.token = CancelToken()
        self.progress = QProgressBar()
        self.progress.setMaximum(1000)
        self.progress.setVisible(False)
        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        self.cancel_btn = QPushButton(tr("Cancel"))
        self.cancel_btn.setObjectName("link")
        self.cancel_btn.setVisible(False)
        self.cancel_btn.clicked.connect(self.token.cancel)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll)
        host = QWidget()
        host.setObjectName("pageBody")
        row = QHBoxLayout(host)
        row.setContentsMargins(56, 20, 56, 20)
        self.body = QWidget()
        self.body.setMaximumWidth(CONTENT_MAX_WIDTH)
        self.body_lay = QVBoxLayout(self.body)
        self.body_lay.setContentsMargins(0, 0, 0, 0)
        self.body_lay.setSpacing(14)
        row.addStretch(1)
        row.addWidget(self.body, 100)
        row.addStretch(1)
        scroll.setWidget(host)
        self._host, self._host_row = host, row

    def _fit(self) -> None:
        """QScrollArea sizes its content from minimumSizeHint, which ignores word-wrapped labels; ask the layout for the real height."""
        m = self._host_row.contentsMargins()
        width = min(self.body.width() or CONTENT_MAX_WIDTH, CONTENT_MAX_WIDTH)
        need = self.body_lay.totalHeightForWidth(width)
        if need > 0:
            self._host.setMinimumHeight(need + m.top() + m.bottom())

    def resizeEvent(self, e) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(e)
        QTimer.singleShot(0, self._fit)

    def showEvent(self, e) -> None:  # noqa: N802 - Qt override
        super().showEvent(e)
        QTimer.singleShot(0, self._fit)

    # ---------------------------------------------------------------- header
    def crumb(self, title: str, *, with_cancel: bool = True) -> QHBoxLayout:
        c = palette(self.theme)
        row = QHBoxLayout()
        row.setSpacing(6)
        chev = QLabel()
        chev.setPixmap(icons.glyph_pixmap("chevron-left", c["accent"], 18, 9))
        chev.setFixedSize(18, 18)
        home = QPushButton(tr("Home"))
        home.setObjectName("crumb")
        home.setCursor(home.cursor())
        home.clicked.connect(lambda: self.win.show_page("home"))
        sep = QLabel("/")
        sep.setObjectName("crumbMuted")
        cur = QLabel(title)
        cur.setObjectName("crumbMuted")
        for w in (chev, home, sep, cur):
            row.addWidget(w)
        row.addStretch(1)
        if with_cancel:
            row.addWidget(self.cancel_btn)
        return row

    def header(self, title: str, subtitle: str = "", *, card: bool = True, with_cancel: bool = True) -> QVBoxLayout:
        """Breadcrumb + big title + lead text. Returns the layout to fill: a white card by default, the page body if card=False."""
        self.body_lay.addLayout(self.crumb(title, with_cancel=with_cancel))
        t = QLabel(title)
        t.setObjectName("title")
        t.setWordWrap(True)
        self.body_lay.addWidget(t)
        if subtitle:
            lead = QLabel(subtitle)
            lead.setObjectName("lead")
            lead.setWordWrap(True)
            lead.setMaximumWidth(900)
            self.body_lay.addWidget(lead)
        if not card:
            return self.body_lay
        c = Card()
        self.body_lay.addWidget(c)
        self.body_lay.addStretch(1)
        return c.lay

    # ---------------------------------------------------------------- background work
    def _busy(self, on: bool, label: str = "") -> None:
        self.cancel_btn.setVisible(on)
        self.progress.setVisible(on)
        if on:
            self.progress.setValue(0)
            if label:
                self.status.setText(label)

    def _set_permille(self, v: int) -> None:
        self.progress.setValue(v)

    def run_bg(self, fn, on_done, label: str = "") -> None:
        t = QThread(self)
        w = Worker(fn)
        w.moveToThread(t)
        t.started.connect(w.run)

        def on_progress(d: int, tot: int) -> None:
            # permille, not raw bytes: QProgressBar holds a 32-bit int and a 5 TB disk is ~5e12 bytes
            self._set_permille(min(1000, int(1000 * d / tot)) if tot else 0)

        def finished_ok(result: object) -> None:
            self._busy(False)
            on_done(result)
            t.quit()
            t.wait(3000)  # let the worker thread finish before anything can destroy it

        def finished_bad(msg: str) -> None:
            self._busy(False)
            self.on_error(msg)
            t.quit()
            t.wait(3000)

        def cleanup() -> None:
            if t in self._threads:
                self._threads.remove(t)

        w.progress.connect(on_progress)
        w.done.connect(finished_ok)
        w.failed.connect(finished_bad)
        t.finished.connect(cleanup)
        self._threads.append(t)
        self._workers.append(w)
        self.token.reset()
        self._busy(True, label)
        t.start()

    def on_error(self, msg: str) -> None:
        self.status.setText(msg)
        QMessageBox.warning(self, NAME, msg)


def pick_source(parent: QWidget, edit: QLineEdit) -> None:
    path, _ = QFileDialog.getOpenFileName(
        parent, tr("Choose a disk image"), "", "Disk images (*.img *.dd *.raw *.vhd *.vhdx *.vmdk *.vdi *.iso);;All files (*)")
    if path:
        edit.setText(path)


class SourceRow(QWidget):
    """Choose an image file or a physical disk: a text box with 'Detect disks' and 'Browse image...' underneath."""

    def __init__(self, inline: bool = False) -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(tr("Image file (.img/.vhd) or \\\\.\\PhysicalDriveN"))
        self.combo = QComboBox()
        self.combo.setVisible(False)
        self.combo.activated.connect(lambda _i: self.edit.setText(self.combo.currentData() or ""))
        detect = QPushButton(tr("Detect disks"))
        detect.clicked.connect(self.detect)
        browse = QPushButton(tr("Browse image..."))
        browse.clicked.connect(lambda: pick_source(self, self.edit))
        if inline:  # [ text box ........ ] [Detect disks] [Browse image...]
            row = QHBoxLayout()
            row.setSpacing(12)
            row.addWidget(self.edit, 1)
            row.addWidget(detect)
            row.addWidget(browse)
            lay.addLayout(row)
        else:  # text box, then two equal buttons underneath
            btns = QHBoxLayout()
            btns.setSpacing(12)
            btns.addWidget(detect, 1)
            btns.addWidget(browse, 1)
            lay.addWidget(self.edit)
            lay.addLayout(btns)
        lay.addWidget(self.combo)
        self.setMinimumHeight(48 if inline else 48 + 12 + 46)  # edit + gap + button row, so a tight layout cannot clip it

    def detect(self) -> None:
        try:
            items = disks.list_disks()
        except Exception as ex:  # noqa: BLE001
            QMessageBox.warning(self, NAME, str(ex))
            return
        self.combo.clear()
        for d in items:
            self.combo.addItem(f"{d.path}  {d.model}  {d.size // 2 ** 30} GiB" + ("  [system]" if d.is_system else ""), d.path)
        self.combo.setVisible(bool(items))
        if not items:
            QMessageBox.information(self, NAME, tr("No disks were listed. Run Lavora as administrator to see physical disks."))

    @property
    def path(self) -> str:
        return self.edit.text().strip()
