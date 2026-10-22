"""'I deleted files': choose source -> scan -> review and preview -> recover (the main wizard)."""
from __future__ import annotations

import itertools
import threading

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..brand import NAME
from ..core import license, safety
from ..core import partitions as parts
from ..core.device import BlockSource, PartitionView, open_source
from ..core.fs import FsError, open_filesystem
from ..core.fs.base import FileEntry
from ..core.recovery import Estimate, iter_entries, preview, recover
from ..i18n import tr
from . import icons
from .base import Page, SourceRow
from .widgets import CHANCE_ROLE, Banner, Card, ChanceDelegate, ChoiceCard, Stepper, palette

IMAGE_EXT = {"jpg", "jpeg", "png", "gif", "bmp", "webp", "heic", "tif", "tiff", "raw", "cr2", "nef"}
DOC_EXT = {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv", "odt", "ods", "rtf", "md", "zip"}
VIDEO_EXT = {"mp4", "mov", "avi", "mkv", "wmv", "m4v", "mpg", "mpeg", "3gp"}
CHIPS = [("All", None), ("Images", IMAGE_EXT), ("Documents", DOC_EXT), ("Video", VIDEO_EXT)]
MAX_ROWS = 5000


def fmt_size(n: float) -> str:
    n = float(n)
    for unit, step in (("B", 1), ("KB", 1000), ("MB", 1000 ** 2), ("GB", 1000 ** 3), ("TB", 1000 ** 4)):
        nxt = step * 1000
        if n < nxt or unit == "TB":
            return f"{n:.0f} B" if unit == "B" else f"{n / step:.1f} {unit}" if n / step < 100 else f"{n / step:.0f} {unit}"
    return str(n)


def chance_for(e: FileEntry) -> tuple[str, tuple[float, str, str] | None]:
    """(state text, (percent, label, level)) for a table row. level drives the bar colour: high / medium / low."""
    if not e.deleted:
        return tr("Present"), None
    if e.note.startswith("unsupported"):
        return tr("Unsupported"), (0.0, tr("Unknown"), "low")
    if e.overwritten_pct >= 100:
        return tr("Overwritten"), (0.0, tr("Lost") + " · 0%", "low")
    pct = min(99.0, 100.0 - e.overwritten_pct)
    level = "high" if pct >= 85 else "medium" if pct >= 40 else "low"
    state = tr("Deleted") if e.overwritten_pct == 0 else tr("Partly overwritten") if e.overwritten_pct < 60 else tr("Mostly overwritten")
    return state, (pct, f"{tr(level.capitalize())} · {pct:.0f}%", level)


class _PreviewBridge(QObject):
    ready = Signal(int, object)


class RecoverPage(Page):
    MAX_ENTRIES = 2_000_000  # the list lives in memory; a 5 TB disk can hold more files than that

    def __init__(self, win):
        super().__init__(win)
        self.c = palette(self.theme)
        self.header(tr("I deleted files"), tr("Scan a disk or image for deleted files, preview them, and recover them to a different drive. Nothing is written to the source."),
                    card=False, with_cancel=False)
        self.stepper = Stepper([tr("Choose source"), tr("Scan"), tr("Review and preview"), tr("Recover")], self.theme)
        self.body_lay.addWidget(self.stepper)
        self.body_lay.addSpacing(6)

        self.entries: list[FileEntry] = []
        self.shown: list[FileEntry] = []
        self.carved: list = []
        self.fs = None
        self.vol: BlockSource | None = None
        self._ext_filter: set[str] | None = None
        self._preview_gen = 0
        self._bridge = _PreviewBridge()
        self._bridge.ready.connect(self._preview_ready)

        cols = QHBoxLayout()
        cols.setSpacing(24)
        cols.addWidget(self._build_left(), 0, Qt.AlignmentFlag.AlignTop)
        cols.addLayout(self._build_right(), 1)
        self.body_lay.addLayout(cols)
        self.body_lay.addStretch(1)
        self._update_selection()
        self._update_empty()

    # ---------------------------------------------------------------- left column
    def _build_left(self) -> QWidget:
        card = Card((28, 26, 28, 24), 12)
        card.setFixedWidth(462)
        self.left = QStackedWidget()
        card.lay.addWidget(self.left)

        form = QWidget()
        f = QVBoxLayout(form)
        f.setContentsMargins(0, 0, 0, 0)
        f.setSpacing(12)
        f.addWidget(self._label(tr("Disk or image")))
        self.src = SourceRow()
        f.addWidget(self.src)
        f.addSpacing(6)
        f.addWidget(self._label(tr("Volume")))
        self.part = QComboBox()
        self.part.addItem(tr("Auto-detect volume"), None)
        f.addWidget(self.part)
        vol_btn = QPushButton(tr("List volumes"))
        vol_btn.clicked.connect(self.list_volumes)
        vrow = QHBoxLayout()
        vrow.addWidget(vol_btn)
        vrow.addStretch(1)
        f.addLayout(vrow)
        f.addSpacing(6)
        f.addWidget(self._label(tr("Scan mode")))
        self.mode_files = ChoiceCard(tr("Scan for deleted files"), tr("Reads the file system for deleted entries. Fast, and keeps names and folders."), self.theme)
        self.mode_deep = ChoiceCard(tr("Deep scan (by file type)"), tr("Searches the whole disk for file signatures. Slower, and original names are usually lost."), self.theme)
        self.mode_files.group = self.mode_deep.group = [self.mode_files, self.mode_deep]
        self.mode_files.set_selected(True)
        f.addWidget(self.mode_files)
        f.addWidget(self.mode_deep)
        self.include_live = QCheckBox(tr("Also list existing files"))
        hint = QLabel(tr("Slower and uses more memory."))
        hint.setObjectName("muted")
        crow = QVBoxLayout()
        crow.setSpacing(0)
        crow.addWidget(self.include_live)
        hint.setContentsMargins(30, 0, 0, 0)
        crow.addWidget(hint)
        f.addSpacing(4)
        f.addLayout(crow)
        self.start_btn = QPushButton(tr("Start scan"))
        self.start_btn.setObjectName("primary")
        self.start_btn.clicked.connect(self.start_scan)
        f.addSpacing(6)
        f.addWidget(self.start_btn)
        f.addWidget(self._lock_note())
        self.left.addWidget(form)

        det = QWidget()
        d = QVBoxLayout(det)
        d.setContentsMargins(0, 0, 0, 0)
        d.setSpacing(12)
        title = QLabel(tr("Scan details"))
        title.setObjectName("h3")
        d.addWidget(title)
        self.det_labels: dict[str, QLabel] = {}
        for key, name in (("source", tr("Source")), ("volume", tr("Volume")), ("mode", tr("Mode"))):
            k = QLabel(name)
            k.setObjectName("muted")
            v = QLabel("-")
            v.setObjectName("fieldLabel")
            v.setWordWrap(True)
            v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.det_labels[key] = v
            col = QVBoxLayout()
            col.setSpacing(2)
            col.addWidget(k)
            col.addWidget(v)
            d.addLayout(col)
        change = QPushButton(tr("Change source"))
        change.clicked.connect(self.change_source)
        self.deep_btn = QPushButton(tr("Run a deep scan"))
        self.deep_btn.clicked.connect(self.carve)
        d.addSpacing(6)
        d.addWidget(change)
        d.addWidget(self.deep_btn)
        d.addWidget(self._lock_note())
        d.addStretch(1)
        self.left.addWidget(det)
        return card

    def _label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("fieldLabel")
        return lbl

    def _lock_note(self) -> QWidget:
        w = QWidget()
        r = QHBoxLayout(w)
        r.setContentsMargins(0, 4, 0, 0)
        r.setSpacing(8)
        ic = QLabel()
        ic.setPixmap(icons.glyph_pixmap("lock", self.c["ok"], 16, 8))
        ic.setFixedSize(16, 16)
        t = QLabel(tr("The source is opened read-only."))
        t.setObjectName("muted")
        r.addWidget(ic)
        r.addWidget(t, 1)
        return w

    # ---------------------------------------------------------------- right column
    def _build_right(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(18)

        results = Card((0, 0, 0, 0), 0)
        self.banner = Banner(self.theme)
        self.banner.cancel.clicked.connect(self.token.cancel)
        results.lay.addWidget(self.banner)
        frow_host = QWidget()
        frow = QHBoxLayout(frow_host)
        frow.setContentsMargins(28, 24, 28, 18)
        frow.setSpacing(12)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText(tr("Filter by name or type, e.g. jpg"))
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(300)
        self._debounce.timeout.connect(self.apply_filter)
        self.filter.textChanged.connect(lambda _t: self._debounce.start())
        frow.addWidget(self.filter, 1)
        self.chip_btns: list[QPushButton] = []
        for label, exts in CHIPS:
            b = QPushButton(tr(label))
            b.setObjectName("chipBtn")
            b.setCheckable(True)
            b.setChecked(exts is None)
            b.clicked.connect(lambda _c=False, e=exts, btn=b: self._chip(e, btn))
            self.chip_btns.append(b)
            frow.addWidget(b)
        results.lay.addWidget(frow_host)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["", tr("NAME"), tr("SIZE"), tr("STATE"), tr("CHANCE"), tr("PATH")])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(54)
        hh = self.table.horizontalHeader()
        hh.setHighlightSections(False)
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hh.setStretchLastSection(True)
        hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        for i, w in enumerate((60, 235, 100, 175, 225, 200)):
            self.table.setColumnWidth(i, w)
        self.table.setItemDelegateForColumn(4, ChanceDelegate(self.theme))
        self.table.setMinimumHeight(430)
        self.table.itemChanged.connect(lambda _i: self._update_selection())
        self.table.itemSelectionChanged.connect(self.show_preview)
        self.empty = self._empty_state()
        self.empty.setParent(self.table.viewport())
        self.table.viewport().installEventFilter(self)
        results.lay.addWidget(self.table)
        col.addWidget(results)

        # preview
        self.preview_card = Card((28, 24, 28, 24), 0)
        self.preview_stack = QStackedWidget()
        ph = QWidget()
        pl = QVBoxLayout(ph)
        pl.setContentsMargins(0, 28, 0, 28)
        pt = QLabel(tr("Preview"))
        pt.setObjectName("h3")
        pt.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ps = QLabel(tr("Select a file in the results to preview it before recovering."))
        ps.setObjectName("lead")
        ps.setStyleSheet("font-size: 17px;")
        ps.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pl.addWidget(pt)
        pl.addWidget(ps)
        self.preview_stack.addWidget(ph)
        detail = QWidget()
        dl = QHBoxLayout(detail)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.setSpacing(28)
        box = QFrame()
        box.setObjectName("preview")
        box.setFixedSize(240, 170)
        bl = QVBoxLayout(box)
        bl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.prev_icon = QLabel()
        self.prev_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.prev_icon.setPixmap(icons.glyph_pixmap("file", self.c["muted"], 40, 5))
        self.prev_text = QLabel(tr("File preview"))
        self.prev_text.setObjectName("muted")
        self.prev_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.prev_text.setWordWrap(True)
        bl.addWidget(self.prev_icon)
        bl.addWidget(self.prev_text)
        dl.addWidget(box)
        info = QVBoxLayout()
        self.prev_name = QLabel("")
        self.prev_name.setStyleSheet("font-size: 22px; font-weight: 700;")
        self.prev_name.setWordWrap(True)
        info.addWidget(self.prev_name)
        grid = QGridLayout()
        grid.setHorizontalSpacing(28)
        grid.setVerticalSpacing(8)
        self.prev_fields: dict[str, QLabel] = {}
        for r, (key, name) in enumerate((("size", tr("Size")), ("state", tr("State")), ("chance", tr("Recovery chance")), ("path", tr("Original path")))):
            k = QLabel(name)
            k.setObjectName("muted")
            v = QLabel("")
            v.setWordWrap(True)
            v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.prev_fields[key] = v
            grid.addWidget(k, r, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(v, r, 1)
        grid.setColumnStretch(1, 1)
        info.addLayout(grid)
        info.addStretch(1)
        dl.addLayout(info, 1)
        self.preview_stack.addWidget(detail)
        self.preview_card.lay.addWidget(self.preview_stack)
        col.addWidget(self.preview_card)

        # action bar
        bar = Card((28, 18, 24, 18), 0)
        row = QHBoxLayout()
        left = QVBoxLayout()
        left.setSpacing(2)
        self.sel_title = QLabel(tr("No files selected"))
        self.sel_title.setStyleSheet("font-size: 18px; font-weight: 700;")
        sub = QHBoxLayout()
        self.sel_hint = QLabel(tr("You will choose a different drive to save to."))
        self.sel_hint.setObjectName("muted")
        self.sel_all = QPushButton(tr("Select all shown"))
        self.sel_all.setObjectName("link")
        self.sel_all.clicked.connect(lambda: self._set_all(True))
        self.sel_none = QPushButton(tr("Clear"))
        self.sel_none.setObjectName("link")
        self.sel_none.clicked.connect(lambda: self._set_all(False))
        sub.addWidget(self.sel_hint)
        sub.addWidget(self.sel_all)
        sub.addWidget(self.sel_none)
        sub.addStretch(1)
        left.addWidget(self.sel_title)
        left.addLayout(sub)
        self.recover_btn = QPushButton(tr("Recover selected to..."))
        self.recover_btn.setObjectName("primary")
        self.recover_btn.setMinimumWidth(280)
        self.recover_btn.clicked.connect(self.do_recover)
        row.addLayout(left, 1)
        row.addWidget(self.recover_btn)
        bar.lay.addLayout(row)
        col.addWidget(bar)
        col.addWidget(self.status)
        return col

    def _empty_state(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tile = QLabel()
        tile.setPixmap(icons.tile_pixmap("search", self.c["accent"], 72, soft=self.c["accent_soft"]))
        tile.setFixedSize(72, 72)
        t = QLabel(tr("No scan yet"))
        t.setObjectName("h3")
        t.setStyleSheet("font-size: 21px; font-weight: 700;")
        s = QLabel(tr("Choose a source and start a scan. Files appear here as they are found, with a recovery chance for each."))
        s.setObjectName("lead")
        s.setStyleSheet("font-size: 17px;")
        s.setWordWrap(True)
        s.setMaximumWidth(460)
        s.setAlignment(Qt.AlignmentFlag.AlignCenter)
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(tile, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addSpacing(10)
        lay.addWidget(t)
        lay.addWidget(s, 0, Qt.AlignmentFlag.AlignHCenter)
        return w

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802 - Qt override
        if obj is self.table.viewport() and ev.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self.empty.setGeometry(self.table.viewport().rect())
        return super().eventFilter(obj, ev)

    def _update_empty(self) -> None:
        self.empty.setVisible(self.table.rowCount() == 0)
        self.empty.setGeometry(self.table.viewport().rect())

    # ---------------------------------------------------------------- progress goes into the banner on this page
    def _busy(self, on: bool, label: str = "") -> None:
        if on:
            self.banner.bar.setValue(0)
            self.banner.show_message("info", label or tr("Working..."), progress=True, cancellable=True)
        else:
            self.banner.bar.setVisible(False)
            self.banner.cancel.setVisible(False)
        self.start_btn.setEnabled(not on)
        self.deep_btn.setEnabled(not on)

    def _set_permille(self, v: int) -> None:
        self.banner.bar.setValue(v)

    def on_error(self, msg: str) -> None:
        self.banner.show_message("error", msg)
        self.status.setText("")

    # ---------------------------------------------------------------- source / volume
    def _volume(self) -> BlockSource:
        src = open_source(self.src.path)
        sel = self.part.currentData()
        if sel is not None:
            return PartitionView(src, sel[0], sel[1])
        if parts.identify_fs(src, 0):
            return src
        _s, plist = parts.read_partitions(src)
        plist = [p for p in plist if p.fs]
        if len(plist) == 1:
            return PartitionView(src, plist[0].start, plist[0].size)
        raise FsError(tr("Several volumes (or none) found. Use 'List volumes' and pick one."))

    def list_volumes(self) -> None:
        try:
            with open_source(self.src.path) as src:
                scheme, plist = parts.read_partitions(src)
                found = [] if plist else parts.scan_lost_partitions(src, max_bytes=256 * 1024 * 1024)
            self.part.clear()
            self.part.addItem(tr("Auto-detect volume"), None)
            for p in plist:
                self.part.addItem(f"#{p.index} {p.fs or '?'} {p.size // 2 ** 20} MiB ({scheme})", (p.start, p.size))
            for f in found:
                self.part.addItem(f"{tr('found')}: {f.fs} {f.size // 2 ** 20} MiB ({f.confidence})", (f.start, f.size))
        except Exception as ex:  # noqa: BLE001
            QMessageBox.warning(self, NAME, str(ex))

    def change_source(self) -> None:
        self.left.setCurrentIndex(0)
        self.stepper.set_step(1)

    def _show_details(self, mode: str) -> None:
        self.det_labels["source"].setText(self.src.path or "-")
        self.det_labels["volume"].setText(self.part.currentText())
        self.det_labels["mode"].setText(mode)
        self.left.setCurrentIndex(1)

    # ---------------------------------------------------------------- scanning
    def start_scan(self) -> None:
        if not self.src.path:
            QMessageBox.information(self, NAME, tr("Choose a disk or image first."))
            return
        self.carve() if self.mode_deep.selected else self.scan()

    def scan(self) -> None:
        live = self.include_live.isChecked()
        self.stepper.set_step(2)
        self._show_details(tr("Scan for deleted files"))

        def job(progress):
            vol = self._volume()
            fs = open_filesystem(vol)
            out = list(itertools.islice(fs.scan(include_live=live, progress=progress, cancel=self.token), self.MAX_ENTRIES + 1))
            return vol, fs, out
        self.run_bg(job, self.scanned, tr("Scanning for deleted files... (you can cancel; what was found so far is kept)"))

    def scanned(self, res) -> None:
        self.vol, self.fs, self.entries = res
        capped = len(self.entries) > self.MAX_ENTRIES
        if capped:
            self.entries = self.entries[:self.MAX_ENTRIES]
        self.carved = []
        est = Estimate()
        for e in self.entries:
            est.add(e)
        n = est.deleted if not self.include_live.isChecked() else est.files
        word = tr("deleted files") if not self.include_live.isChecked() else tr("files")
        if self.token.cancelled:
            self.banner.show_message("warn", tr("Scan stopped: partial list, {n} {w} so far").format(n=n, w=word))
        else:
            extra = (" · " + tr("list capped at {n}; use the CLI for the rest").format(n=self.MAX_ENTRIES)) if capped else ""
            self.banner.show_message("success", tr("Scan complete: {n} {w} found").format(n=n, w=word) + extra)
        self.stepper.set_step(3)
        self.apply_filter()

    def carve(self) -> None:
        from ..core import carver
        self.stepper.set_step(2)
        self._show_details(tr("Deep scan (by file type)"))

        def job(progress):
            src = open_source(self.src.path)
            found = list(itertools.islice(carver.carve(src, progress=progress, cancel=self.token), self.MAX_ENTRIES))
            return src, found
        self.run_bg(job, self.carved_done, tr("Searching the whole disk for file signatures... (cancel any time; found files are kept)"))

    def carved_done(self, res) -> None:
        self.vol, self.carved = res
        self.entries, self.fs, self.shown = [], None, []
        self._fill_table_carved()
        if self.token.cancelled:
            self.banner.show_message("warn", tr("Scan stopped: {n} files found by signature so far").format(n=len(self.carved)))
        else:
            self.banner.show_message("success", tr("Deep scan complete: {n} files found by signature").format(n=len(self.carved)))
        self.stepper.set_step(3)

    # ---------------------------------------------------------------- results table
    def _chip(self, exts: set[str] | None, btn: QPushButton) -> None:
        for b in self.chip_btns:
            b.setChecked(b is btn)
        self._ext_filter = exts
        self.apply_filter()

    def _row(self, name: str, size: int, state: str, chance, path: str) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        chk = QTableWidgetItem()
        chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        chk.setCheckState(Qt.CheckState.Unchecked)
        self.table.setItem(r, 0, chk)
        name_item = QTableWidgetItem(name)
        name_item.setToolTip(name)
        name_item.setData(Qt.ItemDataRole.FontRole, None)
        self.table.setItem(r, 1, name_item)
        self.table.setItem(r, 2, QTableWidgetItem(fmt_size(size)))
        self.table.setItem(r, 3, QTableWidgetItem(state))
        ch = QTableWidgetItem("-" if chance is None else "")
        if chance is not None:
            ch.setData(CHANCE_ROLE, chance)
        self.table.setItem(r, 4, ch)
        p = QTableWidgetItem(path)
        p.setToolTip(path)
        self.table.setItem(r, 5, p)

    def apply_filter(self) -> None:
        if self.carved:
            return
        exts = self._ext_filter
        shown = list(itertools.islice(iter_entries(self.entries, exts=exts, name_contains=self.filter.text().strip()), MAX_ROWS))
        self.shown = shown
        self.table.blockSignals(True)
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(0)
        for e in shown:
            state, chance = chance_for(e)
            self._row(e.name, e.size, state, chance, "\\" + e.path.replace("/", "\\").rsplit("\\", 1)[0] if "/" in e.path else "\\")
        self.table.setUpdatesEnabled(True)
        self.table.blockSignals(False)
        self._update_selection()
        self._update_empty()

    def _fill_table_carved(self) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for c in self.carved[:MAX_ROWS]:
            self._row(c.name, c.size, tr("Carved"), None, f"offset {c.offset}")
        self.table.blockSignals(False)
        self._update_selection()
        self._update_empty()

    def _checked_rows(self) -> list[int]:
        return [r for r in range(self.table.rowCount())
                if (it := self.table.item(r, 0)) is not None and it.checkState() == Qt.CheckState.Checked]

    def _row_size(self, r: int) -> int:
        if self.carved:
            return self.carved[r].size
        return self.shown[r].size if r < len(self.shown) else 0

    def _update_selection(self) -> None:
        rows = self._checked_rows()
        total = sum(self._row_size(r) for r in rows)
        if rows:
            self.sel_title.setText(tr("{n} files selected · {s}").format(n=len(rows), s=fmt_size(total)))
        else:
            self.sel_title.setText(tr("No files selected"))
        self.recover_btn.setEnabled(bool(rows))
        has = self.table.rowCount() > 0
        self.sel_all.setVisible(has)
        self.sel_none.setVisible(has)
        if has and not self.carved:
            self.stepper.set_step(3) if self.stepper.current < 4 else None

    def _set_all(self, on: bool) -> None:
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item is not None:
                item.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        self.table.blockSignals(False)
        self._update_selection()

    # ---------------------------------------------------------------- preview (loaded in the background so a slow disk never freezes the window)
    def show_preview(self) -> None:
        rows = {i.row() for i in self.table.selectedItems()}
        if len(rows) != 1:
            return
        r = next(iter(rows))
        self.preview_stack.setCurrentIndex(1)
        if self.carved:
            c = self.carved[r]
            self.prev_name.setText(c.name)
            self._fields(fmt_size(c.size), tr("Carved"), "-", f"offset {c.offset}")
            self._set_preview_text(tr("File preview"))
            return
        if r >= len(self.shown):
            return
        e = self.shown[r]
        state, chance = chance_for(e)
        self.prev_name.setText(e.name)
        self._fields(fmt_size(e.size), state, chance[1] if chance else "-", "\\" + e.path.replace("/", "\\").rsplit("\\", 1)[0] if "/" in e.path else "\\")
        self._set_preview_text(tr("Loading preview..."))
        self._preview_gen += 1
        gen, fs = self._preview_gen, self.fs

        def work() -> None:
            try:
                res = preview(fs, e, limit=16 * 1024)
            except Exception as ex:  # noqa: BLE001
                res = {"kind": "error", "data": str(ex)}
            self._bridge.ready.emit(gen, res)
        threading.Thread(target=work, daemon=True).start()

    def _fields(self, size: str, state: str, chance: str, path: str) -> None:
        for k, v in (("size", size), ("state", state), ("chance", chance), ("path", path)):
            self.prev_fields[k].setText(v)

    def _set_preview_text(self, text: str, pix: QPixmap | None = None) -> None:
        if pix is not None:
            self.prev_icon.setPixmap(pix)
            self.prev_text.setText("")
        else:
            self.prev_icon.setPixmap(icons.glyph_pixmap("file", self.c["muted"], 40, 5))
            self.prev_text.setText(text)

    def _preview_ready(self, gen: int, res: dict) -> None:
        if gen != self._preview_gen:
            return  # the user already selected something else
        kind, data = res["kind"], res["data"]
        if kind == "image":
            pm = QPixmap()
            if pm.loadFromData(data):
                self._set_preview_text("", pm.scaled(220, 150, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
                return
            self._set_preview_text(tr("Image preview not available"))
        elif kind == "text":
            self._set_preview_text(data[:140].strip() or tr("(empty)"))
        elif kind == "hex":
            self._set_preview_text(tr("Binary file"))
        else:
            self._set_preview_text(str(data)[:140])

    # ---------------------------------------------------------------- recovery
    def do_recover(self) -> None:
        rows = self._checked_rows()
        if not rows or not self.vol:
            QMessageBox.information(self, NAME, tr("Tick the files to recover first."))
            return
        out = QFileDialog.getExistingDirectory(self, tr("Recover to (choose a different drive)"))
        if not out:
            return
        lic = license.current()
        self.stepper.set_step(4)

        def job(progress):
            if self.carved:
                from ..core import carver
                safety.check_output_location(self.vol, out)
                done = []
                for r in rows:
                    if self.token():
                        break
                    done.append(carver.extract(self.vol, self.carved[r], out))
                return done
            return recover(self.fs, [self.shown[r] for r in rows], out, source=self.vol, license=lic, cancel=self.token,
                           byte_progress=progress)  # progress inside the current file, so a 2 TB file shows movement

        def finished(rep) -> None:
            n = len(rep) if isinstance(rep, list) else rep.recovered
            if not isinstance(rep, list) and (rep.cancelled or rep.stopped_reason or rep.failed):
                msg = tr("Recovered {n} file(s) to {p}").format(n=n, p=out)
                msg += " · " + (tr("cancelled") if rep.cancelled else rep.stopped_reason or tr("{f} failed").format(f=rep.failed))
                self.banner.show_message("warn", msg)
            else:
                self.banner.show_message("success", tr("Recovered {n} file(s) to {p}").format(n=n, p=out))
        self.run_bg(job, finished, tr("Recovering... (you can cancel; no half-written files are left)"))

    def on_deactivate(self) -> None:  # pragma: no cover - hook for future use
        pass


_ = (QObject, Signal)
