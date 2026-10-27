"""'Recover everything': one page, one button. Pick the disk or image and a folder on a different drive; Lavora tries every method."""
from __future__ import annotations

import os

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from ..brand import NAME
from ..core import autorecover, license
from ..core.device import open_source
from ..core.unread import load_unread, unread_total
from ..i18n import tr
from .base import Page, SourceRow
from .parts import NumberedList, Tag
from .recover_page import fmt_size
from .widgets import Card


def _label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("fieldLabel")
    return lbl


class AutoRecoverPage(Page):
    def __init__(self, win):
        super().__init__(win)
        self.header(tr("Recover everything"), tr("Pick the disk or image and a folder on a different drive. Lavora finds the volumes, tries every method it has to open them, and recovers all the files it can. If one method fails it tries the next."), card=False)
        cols = QHBoxLayout()
        cols.setSpacing(28)

        left = QVBoxLayout()
        left.setSpacing(18)
        form = Card((34, 26, 34, 26), 10)
        form.lay.addWidget(_label(tr("Disk or image")))
        self.src = SourceRow(inline=True)
        form.lay.addWidget(self.src)
        form.lay.addSpacing(4)
        form.lay.addWidget(_label(tr("Save the recovered files to")))
        self.dest = QLineEdit()
        self.dest.setPlaceholderText(tr("A folder on a DIFFERENT drive, with enough free space"))
        choose = QPushButton(tr("Choose..."))
        choose.setMinimumWidth(150)
        choose.clicked.connect(lambda: self.dest.setText(QFileDialog.getExistingDirectory(self, tr("Save recovered files to")) or self.dest.text()))
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(self.dest, 1)
        row.addWidget(choose)
        form.lay.addLayout(row)
        form.lay.addWidget(_label(tr("What to recover")))
        self.what = QComboBox()
        for label, key in ((tr("Everything"), ""), (tr("Photos"), "photos"), (tr("Documents"), "documents"), (tr("Videos"), "videos"), (tr("Audio"), "audio")):
            self.what.addItem(label, key)
        form.lay.addWidget(self.what)
        self.live = QCheckBox(tr("Include files that still exist (not only deleted ones)"))
        self.live.setChecked(True)
        self.thorough = QCheckBox(tr("Thorough: also look for lost volumes and search by file content (much slower)"))
        form.lay.addWidget(self.live)
        form.lay.addWidget(self.thorough)
        self.go_btn = QPushButton(tr("Recover everything"))
        self.go_btn.setObjectName("primary")
        self.go_btn.clicked.connect(self.start)
        form.lay.addSpacing(4)
        form.lay.addWidget(self.go_btn)
        self.state_tag = Tag(tr("Ready"), "neutral", self.theme)
        prow = QHBoxLayout()
        prow.addWidget(self.state_tag)
        prow.addWidget(self.status, 1)
        form.lay.addWidget(self.progress)
        form.lay.addLayout(prow)
        left.addWidget(form)

        res = Card((34, 24, 34, 24), 10)
        head = QHBoxLayout()
        h = QLabel(tr("Result"))
        h.setStyleSheet("font-size: 19px; font-weight: 700;")
        head.addWidget(h)
        head.addStretch(1)
        res.lay.addLayout(head)
        self.summary = QLabel(tr("Nothing recovered yet."))
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("font-size: 16px;")
        res.lay.addWidget(self.summary)
        brow = QHBoxLayout()
        brow.setSpacing(12)
        self.open_btn = QPushButton(tr("Open the recovered files"))
        self.open_btn.setEnabled(False)
        self.open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self._out)))
        self.report_btn = QPushButton(tr("Open the report"))
        self.report_btn.setEnabled(False)
        self.report_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self._report)))
        brow.addWidget(self.open_btn)
        brow.addWidget(self.report_btn)
        brow.addStretch(1)
        res.lay.addLayout(brow)
        res.lay.addStretch(1)
        left.addWidget(res, 1)
        cols.addLayout(left, 3)

        right = QVBoxLayout()
        right.setSpacing(18)
        how = Card((30, 24, 30, 24), 12)
        t = QLabel(tr("How it works"))
        t.setStyleSheet("font-size: 19px; font-weight: 700;")
        how.lay.addWidget(t)
        how.lay.addWidget(NumberedList([
            "<b>" + tr("Finds the volumes.") + "</b> " + tr("From the partition table, or by scanning for lost ones."),
            "<b>" + tr("Opens each one, with fallbacks.") + "</b> " + tr("Backup boot sector, MFT mirror, backup superblock..."),
            "<b>" + tr("Recovers every file,") + "</b> " + tr("existing and deleted, keeping folders and names."),
            "<b>" + tr("Searches by content") + "</b> " + tr("when a filesystem cannot be read at all."),
        ], self.theme, size=15))
        right.addWidget(how)
        logc = Card((30, 22, 30, 22), 10)
        lt = QLabel(tr("What was tried"))
        lt.setStyleSheet("font-size: 19px; font-weight: 700;")
        logc.lay.addWidget(lt)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(150)
        self.log.setStyleSheet("background: #1e1b33; color: #f1effa; border: none; border-radius: 14px; padding: 14px; "
                               "font-family: 'JetBrains Mono', 'Cascadia Mono', Consolas, monospace; font-size: 13.5px;")
        self.log.setPlaceholderText(tr("Each method Lavora tries appears here, with its result."))
        logc.lay.addWidget(self.log, 1)
        right.addWidget(logc, 1)
        cols.addLayout(right, 2)
        self.body_lay.addLayout(cols, 1)

        self._events: list[autorecover.Step] = []
        self._shown = 0
        self._stage: tuple[str, int, int] = ("", 0, 0)
        self._out = ""
        self._report = ""
        self._timer = QTimer(self)
        self._timer.setInterval(400)
        self._timer.timeout.connect(self._tick)

    # ---------------------------------------------------------------- entry from other pages
    def prefill(self, source_path: str) -> None:
        self.src.edit.setText(source_path)
        self.status.setText(tr("Choose where to save the recovered files, then press Recover everything."))

    # ---------------------------------------------------------------- running
    def _busy(self, on: bool, label: str = "") -> None:
        self.cancel_btn.setVisible(on)
        self.go_btn.setEnabled(not on)
        self.progress.setVisible(on)
        if on:
            self.progress.setValue(0)
            self.state_tag.set_tone("accent", tr("Running"))
            self._timer.start()
        else:
            self._timer.stop()
            self._tick()

    def _tick(self) -> None:
        while self._shown < len(self._events):
            st = self._events[self._shown]
            mark = {"ok": "OK  ", "failed": "FAIL", "info": "..  ", "skipped": "skip"}.get(st.status, "    ")
            self.log.appendPlainText(f"[{mark}] {st.stage}" + (f"\n        {st.detail}" if st.detail else ""))
            self._shown += 1
        stage, done, total = self._stage
        if stage:
            self.status.setText(f"{stage}" + (f": {100 * done // total}%" if total else f": {done:,} files"))
            if total:
                self.progress.setValue(min(1000, int(1000 * done / total)))

    def start(self) -> None:
        if not self.src.path:
            QMessageBox.information(self, NAME, tr("Choose the disk or image to recover from first."))
            return
        out = self.dest.text().strip()
        if not out:
            QMessageBox.information(self, NAME, tr("Choose a folder on a different drive to save the recovered files to."))
            return
        path, live, thorough = self.src.path, self.live.isChecked(), self.thorough.isChecked()
        only = autorecover.parse_only(self.what.currentData())
        lic = license.current()
        self._events, self._shown, self._stage, self._out, self._report = [], 0, ("", 0, 0), out, ""
        self.log.clear()
        self.summary.setText(tr("Working..."))
        self.open_btn.setEnabled(False)
        self.report_btn.setEnabled(False)

        def job(_progress):
            with open_source(path) as src:
                unread = load_unread(path) if os.path.isfile(path) else None
                if unread:
                    self._events.append(autorecover.Step("Partial image", "info", tr("{s} of this image was never copied; files touching it are kept apart").format(s=fmt_size(unread_total(unread)))))
                return autorecover.auto_recover(src, out, include_live=live, thorough=thorough, license=lic, unread=unread,
                                                progress=lambda s, d, t: setattr(self, "_stage", (s, d, t)), cancel=self.token,
                                                on_step=self._events.append, only_exts=only)

        def done(rep: autorecover.AutoReport) -> None:
            self._stage = ("", 0, 0)
            self.summary.setText(autorecover.summary_text(rep))
            self._report = rep.report_path
            self.open_btn.setEnabled(os.path.isdir(out))
            self.report_btn.setEnabled(bool(rep.report_path))
            if rep.cancelled:
                self.state_tag.set_tone("warn", tr("Stopped"))
            elif rep.recovered:
                self.state_tag.set_tone("ok", tr("Done"))
            else:
                self.state_tag.set_tone("danger", tr("Nothing found"))
            self.status.setText(tr("Finished. See the log for every method that was tried."))

        self.run_bg(job, done, tr("Starting..."))

    def on_error(self, msg: str) -> None:
        self.state_tag.set_tone("danger", tr("Failed"))
        self.summary.setText(msg)
        self.status.setText("")
        QMessageBox.warning(self, NAME, msg)
