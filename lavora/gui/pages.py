"""The task pages: Back up my PC, PC won't boot (Doctor), Disk is failing (imaging), Forgot password, RAID builder."""
from __future__ import annotations

import json
import os
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..brand import NAME
from ..core import backup, doctor, imaging, license, raid, safety, undo
from ..core.device import open_source
from ..i18n import tr
from . import icons
from .base import Page, SourceRow
from .parts import BADGE_ROLE, BadgeDelegate, CheckList, CodeBlock, Notice, NumberedList, RadioDelegate, Tag
from .recover_page import fmt_size
from .widgets import CHANCE_ROLE, Card, ChanceDelegate, Stepper, palette

SEVERITY = {"critical": (tr("Critical"), "danger"), "warning": (tr("Warning"), "warn"), "info": (tr("Info"), "info")}


def _label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("fieldLabel")
    return lbl


def _hint(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("muted")
    lbl.setWordWrap(True)
    return lbl


def _h3(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet("font-size: 19px; font-weight: 700;")
    return lbl


def _lock_note(theme: str, text: str) -> QWidget:
    c = palette(theme)
    w = QWidget()
    r = QHBoxLayout(w)
    r.setContentsMargins(0, 4, 0, 0)
    r.setSpacing(8)
    ic = QLabel()
    ic.setPixmap(icons.glyph_pixmap("lock", c["ok"], 16, 8))
    ic.setFixedSize(16, 16)
    t = QLabel(text)
    t.setObjectName("muted")
    r.addWidget(ic)
    r.addWidget(t, 1)
    return w


def _path_row(edit: QLineEdit, button: QPushButton) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setSpacing(12)
    row.addWidget(edit, 1)
    row.addWidget(button)
    return row


def _style_table(t: QTableWidget, widths: tuple[int, ...], row_h: int = 60) -> None:
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setShowGrid(False)
    t.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    t.setWordWrap(False)
    t.verticalHeader().setVisible(False)
    t.verticalHeader().setDefaultSectionSize(row_h)
    hh = t.horizontalHeader()
    hh.setHighlightSections(False)
    hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    hh.setStretchLastSection(True)
    hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    for i, w in enumerate(widths):
        t.setColumnWidth(i, w)


# ====================================================================================================== Back up my PC
class BackupPage(Page):
    def __init__(self, win):
        super().__init__(win)
        self.header(tr("Back up my PC"), tr("Incremental, compressed, optionally AES-256 encrypted file backups with a test-restore check."), card=False)
        cols = QHBoxLayout()
        cols.setSpacing(32)
        form = Card((36, 32, 36, 32), 12)
        self.folder = QLineEdit()
        self.folder.setPlaceholderText(tr("Choose the folder you want to protect"))
        fb = QPushButton(tr("Choose..."))
        fb.setMinimumWidth(170)
        fb.clicked.connect(lambda: self.folder.setText(QFileDialog.getExistingDirectory(self, tr("Folder to back up")) or self.folder.text()))
        self.dest = QLineEdit()
        self.dest.setPlaceholderText(tr("External drive or network share"))
        db = QPushButton(tr("Choose..."))
        db.setMinimumWidth(170)
        db.clicked.connect(lambda: self.dest.setText(QFileDialog.getExistingDirectory(self, tr("Destination")) or self.dest.text()))
        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.pw.setPlaceholderText(tr("Leave empty for no encryption"))
        self.enc_tag = Tag(tr("Not encrypted"), "neutral", self.theme)
        self.pw.textChanged.connect(self._pw_changed)
        pw_head = QHBoxLayout()
        pw_head.addWidget(_label(tr("Password (optional)")))
        pw_head.addStretch(1)
        pw_head.addWidget(self.enc_tag)
        self.go_btn = QPushButton(tr("Back up and verify"))
        self.go_btn.setObjectName("primary")
        self.go_btn.clicked.connect(self.go)
        form.lay.addWidget(_label(tr("Folder to back up")))
        form.lay.addLayout(_path_row(self.folder, fb))
        form.lay.addSpacing(8)
        form.lay.addWidget(_label(tr("Backup destination")))
        form.lay.addLayout(_path_row(self.dest, db))
        form.lay.addSpacing(8)
        form.lay.addLayout(pw_head)
        form.lay.addWidget(self.pw)
        form.lay.addWidget(_hint(tr("Keep the password somewhere safe. You need it to restore an encrypted backup.")))
        form.lay.addSpacing(10)
        form.lay.addWidget(self.go_btn)
        form.lay.addWidget(self.progress)
        form.lay.addWidget(self.status)
        side = Card((30, 28, 30, 28), 16)
        side.lay.addWidget(_h3(tr("What Lavora does")))
        side.lay.addWidget(NumberedList([
            "<b>" + tr("Incremental.") + "</b> " + tr("After the first run, only changed files are copied."),
            "<b>" + tr("Compressed.") + "</b> " + tr("Backups take less space on the destination."),
            "<b>" + tr("Encrypted if you set a password.") + "</b> AES-256.",
            "<b>" + tr("Verified.") + "</b> " + tr("A test restore confirms the backup can be read back."),
        ], self.theme))
        cols.addWidget(form, 3, Qt.AlignmentFlag.AlignTop)
        cols.addWidget(side, 2, Qt.AlignmentFlag.AlignTop)
        self.body_lay.addLayout(cols)
        self.body_lay.addStretch(1)

    def _pw_changed(self, text: str) -> None:
        if text:
            self.enc_tag.set_tone("ok", tr("AES-256 encrypted"))
        else:
            self.enc_tag.set_tone("neutral", tr("Not encrypted"))

    def go(self) -> None:
        try:
            license.current().require("backup")
        except license.LicenseRequired as ex:
            QMessageBox.information(self, NAME, str(ex))
            return
        folder, dest, pw = self.folder.text().strip(), self.dest.text().strip(), self.pw.text() or None
        if not folder or not dest:
            QMessageBox.information(self, NAME, tr("Choose the folder to back up and the destination first."))
            return

        def job(_progress):
            try:
                repo = backup.Repository.open(dest, pw)
            except backup.BackupError:
                repo = backup.Repository.init(dest, pw)
            st = backup.backup(repo, [folder], cancel=self.token)
            if st.cancelled:
                return st, None
            return st, backup.verify_backup(repo, st.snapshot_id)

        def done(r) -> None:
            st, ver = r
            if ver is None:
                self.status.setText(tr("Cancelled: no snapshot was written; run again to continue (stored data is reused)."))
            else:
                self.status.setText(tr("Backed up {n} files. Test restore: {v}").format(n=st.files, v=tr("OK") if ver.ok else tr("FAILED")))
        self.run_bg(job, done, tr("Backing up..."))


# ====================================================================================================== PC won't boot (Doctor)
class DoctorPage(Page):
    def __init__(self, win):
        super().__init__(win)
        self.header(tr("PC won't boot: Lavora Doctor"), tr("Scans partition tables, boot sectors and boot files, then proposes ranked fixes. Every fix takes an undo snapshot first."), card=False)
        top = Card((36, 28, 36, 28), 10)
        top.lay.addWidget(_label(tr("Disk to diagnose")))
        self.src = SourceRow(inline=True)
        self.run_btn = QPushButton(tr("Diagnose"))
        self.run_btn.setObjectName("primary")
        self.run_btn.setMinimumWidth(160)
        self.run_btn.clicked.connect(self.diagnose)
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(self.src, 1)
        row.addWidget(self.run_btn)
        top.lay.addLayout(row)
        top.lay.addWidget(self.progress)
        self.body_lay.addWidget(top)

        res = Card((0, 0, 0, 0), 0)
        head = QHBoxLayout()
        head.setContentsMargins(32, 24, 32, 18)
        head.addWidget(_h3(tr("Findings, most likely fix first")))
        head.addStretch(1)
        self.count_lbl = QLabel(tr("Run Diagnose to scan the disk."))
        self.count_lbl.setObjectName("muted")
        head.addWidget(self.count_lbl)
        res.lay.addLayout(head)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["", tr("SEVERITY"), tr("PROBLEM"), tr("FIX")])
        _style_table(self.table, (70, 170, 560, 400))
        self.table.setItemDelegateForColumn(0, RadioDelegate(self.theme))
        self.table.setItemDelegateForColumn(1, BadgeDelegate(self.theme))
        self.table.setMinimumHeight(150)
        res.lay.addWidget(self.table)
        self.body_lay.addWidget(res, 1)

        bottom = QHBoxLayout()
        bottom.setSpacing(24)
        act = Card((32, 26, 32, 26), 12)
        self.dry = QCheckBox(tr("Dry run"))
        self.dry.setStyleSheet("font-size: 17px; font-weight: 700;")
        self.dry.setChecked(True)
        self.dry.toggled.connect(self._dry_changed)
        act.lay.addWidget(self.dry)
        act.lay.addWidget(_hint(tr("Show what would change and write nothing.")))
        btns = QHBoxLayout()
        btns.setSpacing(12)
        self.apply_btn = QPushButton(tr("Preview selected fix"))
        self.apply_btn.setObjectName("primary")
        self.apply_btn.clicked.connect(self.apply)
        self.undo_btn = QPushButton(tr("Undo last fix"))
        self.undo_btn.clicked.connect(self.undo_last)
        btns.addWidget(self.apply_btn)
        btns.addWidget(self.undo_btn)
        act.lay.addLayout(btns)
        act.lay.addWidget(Notice(tr("An undo snapshot is saved before any fix is written."), "ok", "shield", self.theme))
        act.lay.addStretch(1)
        logc = Card((32, 26, 32, 26), 14)
        logc.lay.addWidget(_h3(tr("Activity log")))
        self.log = CodeBlock(tr("Nothing has run yet."), min_height=180)
        logc.lay.addWidget(self.log)
        bottom.addWidget(act, 2, Qt.AlignmentFlag.AlignTop)
        bottom.addWidget(logc, 3, Qt.AlignmentFlag.AlignTop)
        self.body_lay.addLayout(bottom)
        self.body_lay.addWidget(self.status)
        self.rows: list = []
        self.last_undo: str | None = None

    def _dry_changed(self, on: bool) -> None:
        self.apply_btn.setText(tr("Preview selected fix") if on else tr("Apply selected fix"))

    def diagnose(self) -> None:
        try:
            license.current().require("doctor")
        except license.LicenseRequired as ex:
            QMessageBox.information(self, NAME, str(ex))
            return
        if not self.src.path:
            QMessageBox.information(self, NAME, tr("Choose a disk or image first."))
            return

        def job(progress):
            with open_source(self.src.path) as s:
                return doctor.diagnose(s, cancel=self.token, progress=progress)
        self.run_bg(job, self.show_findings, tr("Diagnosing..."))

    def show_findings(self, findings) -> None:
        self.rows = [(f, fx) for f in findings for fx in (f.fixes or [None])]
        self.table.setRowCount(0)
        for f, fx in self.rows:
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(""))
            sev = QTableWidgetItem("")
            sev.setData(BADGE_ROLE, SEVERITY.get(f.severity, (f.severity.title(), "info")))
            self.table.setItem(r, 1, sev)
            prob = QTableWidgetItem(f.title)
            prob.setToolTip(f.detail)
            self.table.setItem(r, 2, prob)
            self.table.setItem(r, 3, QTableWidgetItem((fx.title if fx else "-") + ("" if not fx or fx.auto else " " + tr("(manual plan)"))))
        if self.rows:
            self.table.selectRow(0)
            self.count_lbl.setText(tr("{n} finding(s)").format(n=len(findings)))
        else:
            self.count_lbl.setText(tr("No problems found."))
        self.log.set_text(tr("Diagnosis finished: {n} finding(s).").format(n=len(findings)) if findings else tr("Diagnosis finished: no problems found."))

    def apply(self) -> None:
        r = self.table.currentRow()
        if r < 0 or r >= len(self.rows) or self.rows[r][1] is None:
            self.log.set_text(tr("Select a finding that has a fix first."))
            return
        fx = self.rows[r][1]
        if not fx.auto:
            self.log.set_text(tr("This is a plan, not an automatic fix. Run these yourself:") + "\n\n" + "\n".join(fx.commands))
            return
        if not self.dry.isChecked() and self.src.path.lower().startswith("\\\\.\\physicaldrive"):
            from PySide6.QtWidgets import QInputDialog
            text, ok = QInputDialog.getText(self, NAME, tr('This writes to a physical disk. Type "APPLY {id}" to continue.').format(id=fx.id))
            if not ok or text.strip().lower() != f"apply {fx.id}".lower():
                return
        res = doctor.apply_fix(self.src.path, fx, dry_run=self.dry.isChecked())
        if res.applied:
            self.last_undo = res.undo_id
            self.log.set_text(tr("Applied: {t}").format(t=fx.title) + "\n" + tr("Undo id: {u}").format(u=res.undo_id))
        else:
            self.log.set_text(tr("Dry run: nothing will be written.") + "\n" + tr("Would save an undo snapshot of the affected sectors.") + "\n" +
                              tr("Would apply: {t}").format(t=fx.title) + "\n" + ", ".join(tr("{n} bytes at {o}").format(n=n, o=o) for o, n in res.plan) +
                              "\n" + tr("No changes made."))

    def undo_last(self) -> None:
        snaps = undo.list_snapshots()
        if not snaps:
            self.log.set_text(tr("Nothing to undo."))
            return
        target = undo.undo(snaps[-1]["id"])
        self.log.set_text(tr("Restored {t} from snapshot {s}").format(t=target, s=snaps[-1]["id"]))


# ====================================================================================================== Disk is failing
class ImagePage(Page):
    def __init__(self, win):
        super().__init__(win)
        self.header(tr("Disk is failing"), tr("Make a sector-by-sector image first. Bad sectors are mapped and retried. Then scan and recover from the image, not from the failing disk."), card=False)
        self.stepper = Stepper([tr("Choose disk and destination"), tr("Create image"), tr("Recover from the image")], self.theme)
        self.body_lay.addWidget(self.stepper)
        cols = QHBoxLayout()
        cols.setSpacing(32)
        left = QVBoxLayout()
        left.setSpacing(24)
        form = Card((36, 32, 36, 32), 12)
        form.lay.addWidget(_label(tr("Failing disk")))
        self.src = SourceRow()
        form.lay.addWidget(self.src)
        form.lay.addSpacing(8)
        form.lay.addWidget(_label(tr("Where to save the image")))
        self.dest = QLineEdit()
        self.dest.setPlaceholderText(tr("A folder on a different drive"))
        ch = QPushButton(tr("Choose..."))
        ch.setMinimumWidth(150)
        ch.clicked.connect(lambda: self.dest.setText(QFileDialog.getExistingDirectory(self, tr("Save the image to")) or self.dest.text()))
        form.lay.addLayout(_path_row(self.dest, ch))
        form.lay.addWidget(Notice(tr("The destination must be on a different drive from the failing disk, with enough free space for a full copy."), "warn", "info-circle", self.theme))
        form.lay.addSpacing(8)
        self.go_btn = QPushButton(tr("Create image"))
        self.go_btn.setObjectName("primary")
        self.go_btn.clicked.connect(self.go)
        form.lay.addWidget(self.go_btn)
        left.addWidget(form)

        prog = Card((36, 28, 36, 30), 14)
        ph = QHBoxLayout()
        ph.addWidget(_h3(tr("Imaging progress")))
        ph.addStretch(1)
        self.state_tag = Tag(tr("Not started"), "neutral", self.theme)
        ph.addWidget(self.state_tag)
        prog.lay.addLayout(ph)
        prog.lay.addWidget(self.progress)
        self.progress.setVisible(True)
        self.stats: dict[str, QLabel] = {}
        srow = QHBoxLayout()
        srow.setSpacing(36)
        for key, name in (("copied", tr("Copied")), ("bad", tr("Bad sectors mapped")), ("retries", tr("Retries")), ("speed", tr("Speed")), ("left", tr("Time left"))):
            col = QVBoxLayout()
            col.setSpacing(4)
            k = QLabel(name)
            k.setObjectName("lead")
            k.setStyleSheet("font-size: 16px;")
            v = QLabel("—")
            v.setStyleSheet("font-size: 17px; font-weight: 700;")
            self.stats[key] = v
            col.addWidget(k)
            col.addWidget(v)
            srow.addLayout(col)
        srow.addStretch(1)
        prog.lay.addLayout(srow)
        prog.lay.addWidget(self.status)
        left.addWidget(prog)
        cols.addLayout(left, 3)

        why = Card((32, 28, 32, 30), 16)
        why.lay.addWidget(_h3(tr("Why image first")))
        intro = QLabel(tr("Every read puts more wear on a failing disk. Lavora copies it once, then you can scan the copy as many times as you need."))
        intro.setWordWrap(True)
        intro.setObjectName("muted")
        intro.setStyleSheet("font-size: 16px;")
        why.lay.addWidget(intro)
        steps = NumberedList([tr("Reads the disk sector by sector."), tr("Maps bad sectors and retries them."), tr("Saves the image to a different drive."),
                              tr("Hands the image to") + ' <a href="recover" style="color:#5b3fd1;">' + tr("I deleted files") + "</a> " + tr("to scan and recover.")], self.theme, size=16)
        steps.linkActivated.connect(lambda _l: self._to_recover())
        why.lay.addWidget(steps)
        cols.addWidget(why, 2, Qt.AlignmentFlag.AlignTop)
        self.body_lay.addLayout(cols)
        self.body_lay.addStretch(1)

        self._job: imaging.ImageJob | None = None
        self._t0 = 0.0
        self._last = (0.0, 0)
        self._poll = QTimer(self)
        self._poll.setInterval(500)
        self._poll.timeout.connect(self._refresh_stats)
        self._image_path = ""
        self._progress_pair = (0, 0)
        self._seen_done, self._seen_at = 0, 0.0
        self._ema: float | None = None

    def _busy(self, on: bool, label: str = "") -> None:
        self.cancel_btn.setVisible(on)
        self.go_btn.setEnabled(not on)
        if on:
            self.progress.setValue(0)
            self.status.setText(label)
            self.state_tag.set_tone("accent", tr("Running"))
            self.stepper.set_step(2)
            self._t0 = time.monotonic()
            self._last = (self._t0, 0)
            self._seen_done, self._seen_at = 0, self._t0
            self._ema = None
            self._poll.start()
        else:
            self._poll.stop()

    def _set_permille(self, v: int) -> None:
        self.progress.setValue(v)

    def run_bg(self, fn, on_done, label: str = "") -> None:  # keep the bar visible (it is part of the design)
        super().run_bg(fn, on_done, label)
        self.progress.setVisible(True)

    def _refresh_stats(self) -> None:
        job = self._job
        if job is None:
            return
        st = job.state
        size = st.get("size", 0)
        done = min(size, max(st.get("next", 0), self._progress_pair[0]))
        bad = sum(ln for _o, ln in (st.get("suspect", []) + st.get("bad", [])))
        now = time.monotonic()
        if now - self._last[0] >= 0.5:
            inst = (done - self._last[1]) / (now - self._last[0])
            self._last = (now, done)
            # smoothed: a drive that stalls for a minute and then bursts must not flash "0 B/s" and "800 h left"
            self._ema = inst if self._ema is None else 0.15 * inst + 0.85 * self._ema
            avg = done / max(now - self._t0, 1e-6)
            speed = max(self._ema, avg * 0.5) if self._ema is not None else avg
            self.stats["speed"].setText(fmt_size(speed) + "/s")
            remaining = size - done
            if avg > 0 and remaining > 0:
                secs = remaining / avg  # based on the average since the start: stable even across stalls
                self.stats["left"].setText(f"{int(secs // 3600)} h {int(secs % 3600 // 60)} min" if secs >= 3600 else f"{int(secs // 60)} min {int(secs % 60)} s")
        self.stats["copied"].setText(f"{fmt_size(done)} / {fmt_size(size)}")
        if done != self._seen_done:
            self._seen_done, self._seen_at = done, now
        stalled = now - self._seen_at
        if stalled > 8:
            self.status.setText(tr("Waiting for the disk to answer at {p} ({s:.0f} s without progress). The drive may be slow or failing; you can cancel safely and resume later.").format(
                p=fmt_size(job.pos), s=stalled))
        else:
            self.status.setText(tr("Reading at {p}; last block took {t:.2f} s").format(p=fmt_size(job.pos), t=job.last_read_s))
        if job.skipped_slow:
            self.status.setText(self.status.text() + "\n" + tr("The drive is reading very slowly in places. Lavora skips slow areas first ({s} so far) and comes back to them last, so the healthy data is safe sooner. You can stop at any time.").format(s=fmt_size(job.skipped_slow)))
        if job.errors:  # what the drive / Windows actually answered, so a failing disk can be told from a driver problem
            total = sum(job.errors.values())
            self.status.setText(self.status.text() + "\n" + tr("Unreadable areas are skipped first and retried afterwards. The drive reported: {e} ({n} refused reads so far).").format(
                e=job.last_error, n=f"{total:,}"))
        self.stats["bad"].setText(f"{bad // 512:,}")
        self.stats["retries"].setText(str(job.retries))

    def _to_recover(self) -> None:
        page = self.win.pages["recover"]
        if self._image_path:
            page.src.edit.setText(self._image_path)  # type: ignore[attr-defined]
        self.win.show_page("recover")

    def go(self) -> None:
        try:
            license.current().require("imaging")
        except license.LicenseRequired as ex:
            QMessageBox.information(self, NAME, str(ex))
            return
        dest = self.dest.text().strip()
        if not self.src.path:
            QMessageBox.information(self, NAME, tr("Choose the failing disk first (type its path, or use Detect disks)."))
            return
        if not dest:
            QMessageBox.information(self, NAME, tr("Choose where to save the image: a folder on a different drive with enough free space."))
            return
        if os.path.isdir(dest) or not dest.lower().endswith((".img", ".dd", ".raw")):
            base = os.path.splitext(os.path.basename(self.src.path.rstrip("\\/")))[0] or "disk"
            base = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in base)
            dest = os.path.join(dest, f"lavora-{base}.img")
        self._image_path = dest
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)  # the chosen folder may not exist yet
        except OSError as ex:
            QMessageBox.warning(self, NAME, str(ex))
            return

        def job(progress):
            def wrapped(d, t):
                self._progress_pair = (d, t)
                progress(d, t)
            with open_source(self.src.path) as s:
                safety.check_output_location(s, dest)  # never write the image onto the disk being imaged
                self._job = imaging.ImageJob(s, dest, cancel_fn=self.token)
                return self._job.run(wrapped)

        def done(r) -> None:
            self._refresh_stats()
            if r.cancelled:
                self.state_tag.set_tone("warn", tr("Stopped"))
                self.status.setText(tr("Stopped. Press Create image again to resume from where it stopped."))
            elif r.complete:
                self.state_tag.set_tone("ok", tr("Complete"))
                self.stepper.set_step(3)
                self.progress.setValue(1000)
                self.status.setText(tr("Image saved to {p}").format(p=r.path))
            else:
                self.state_tag.set_tone("warn", tr("Finished with bad sectors"))
                self.stepper.set_step(3)
                self.status.setText(tr("Image saved to {p} with {b} unreadable. See the .map file next to it.").format(p=r.path, b=fmt_size(r.bad_bytes)))
        self.run_bg(job, done, tr("Imaging... (you can cancel and resume later)"))

    def on_error(self, msg: str) -> None:
        self.state_tag.set_tone("danger", tr("Failed"))
        self.status.setText(msg)


# ====================================================================================================== Forgot password
class PasswordPage(Page):
    COMMAND = 'lavora-cli account linux <mounted-root> <user> --attest "this is my device" --apply'

    def __init__(self, win):
        super().__init__(win)
        c = palette(self.theme)
        self.header(tr("Forgot password"), tr("Owner-only account access for devices that belong to you."), card=False, with_cancel=False)
        before = Card((36, 30, 36, 30), 18)
        head = QHBoxLayout()
        head.setSpacing(16)
        tile = QLabel()
        tile.setPixmap(icons.tile_pixmap("shield", c["accent"], 48, soft=c["accent_soft"]))
        tile.setFixedSize(48, 48)
        head.addWidget(tile)
        head.addWidget(_h3(tr("Before you start")))
        head.addStretch(1)
        before.lay.addLayout(head)
        before.lay.addWidget(CheckList([tr("You must confirm that this is your own device."),
                                        tr("Microsoft and domain accounts and encrypted volumes are never bypassed."),
                                        tr("Every attempt is written to an audit log.")], self.theme))
        self.body_lay.addWidget(before)

        cols = QHBoxLayout()
        cols.setSpacing(32)
        linux = Card((36, 30, 36, 30), 16)
        lh = QHBoxLayout()
        lh.addWidget(_h3(tr("Linux")))
        lh.addStretch(1)
        lh.addWidget(Tag(tr("Available"), "ok", self.theme))
        linux.lay.addLayout(lh)
        d = _hint(tr("Reset a user's password inside a mounted root from Lavora Rescue, using this command."))
        d.setStyleSheet("font-size: 17px;")
        linux.lay.addWidget(d)
        linux.lay.addWidget(CodeBlock(self.COMMAND))
        copy = QPushButton(tr("Copy command"))
        copy.clicked.connect(self._copy)
        row = QHBoxLayout()
        row.addWidget(copy)
        row.addStretch(1)
        linux.lay.addLayout(row)
        win_c = Card((36, 30, 36, 30), 16)
        wh = QHBoxLayout()
        wh.addWidget(_h3(tr("Windows")))
        wh.addStretch(1)
        wh.addWidget(Tag(tr("Not in this version"), "neutral", self.theme))
        win_c.lay.addLayout(wh)
        w1 = _hint(tr("Local-account reset is not available in this version."))
        w1.setStyleSheet("font-size: 17px;")
        win_c.lay.addWidget(w1)
        w2 = QLabel(tr("Using a Microsoft account? Reset it online at") + ' <a href="https://account.live.com/password/reset" style="color:#5b3fd1;">account.live.com/password/reset</a>.')
        w2.setTextFormat(Qt.TextFormat.RichText)
        w2.setWordWrap(True)
        w2.setOpenExternalLinks(False)
        w2.linkActivated.connect(lambda url: QDesktopServices.openUrl(url))
        w2.setStyleSheet("font-size: 17px;")
        win_c.lay.addWidget(w2)
        win_c.lay.addStretch(1)
        cols.addWidget(linux, 1, Qt.AlignmentFlag.AlignTop)
        cols.addWidget(win_c, 1, Qt.AlignmentFlag.AlignTop)
        self.body_lay.addLayout(cols)
        self.body_lay.addStretch(1)

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self.COMMAND)


# ====================================================================================================== RAID builder
LAYOUT_NAMES = {"ls": "Left-symmetric", "la": "Left-asymmetric", "rs": "Right-symmetric", "ra": "Right-asymmetric"}
REDUNDANCY = {0: "No redundancy: every member is needed.", 1: "Mirrored: any one member holds all the data.",
              5: "If one member is missing, it can be rebuilt from parity.", 6: "If up to two members are missing, they can be rebuilt from parity.",
              10: "Mirrored pairs: one member of each pair may be missing."}


def layout_name(c) -> str:
    return {0: "Striped", 1: "Mirrored", 10: "Near-2 mirrors"}.get(c.level) or LAYOUT_NAMES.get(c.layout, c.layout)


class RaidPage(Page):
    def __init__(self, win):
        super().__init__(win)
        self.header(tr("RAID builder"), tr("Add the member disks or images in any order. Lavora finds the RAID level, chunk size, layout and member order, then you can scan and recover from the rebuilt array. Missing members are rebuilt from parity when the level allows."), card=False)
        self.stepper = Stepper([tr("Add members"), tr("Detect parameters"), tr("Scan the rebuilt array")], self.theme)
        self.body_lay.addWidget(self.stepper)
        cols = QHBoxLayout()
        cols.setSpacing(32)
        left = Card((36, 32, 36, 28), 12)
        left.setFixedWidth(470)
        left.lay.addWidget(_label(tr("Members")))
        self.members = QPlainTextEdit()
        self.members.setPlaceholderText(tr("One member per line: image file or \\\\.\\PhysicalDriveN"))
        self.members.setStyleSheet("font-family: 'JetBrains Mono', 'Cascadia Mono', Consolas, monospace; font-size: 14px;")
        self.members.setMinimumHeight(150)
        self.members.textChanged.connect(lambda: self.stepper.set_step(1) if not self.cands else None)
        left.lay.addWidget(self.members)
        add = QPushButton(tr("Add images..."))
        add.clicked.connect(self.add_files)
        arow = QHBoxLayout()
        arow.addWidget(add)
        arow.addStretch(1)
        left.lay.addLayout(arow)
        left.lay.addSpacing(6)
        left.lay.addWidget(_label(tr("RAID level")))
        self.level = QComboBox()
        self.level.addItem(tr("Auto-detect level"), None)
        for lv in (0, 1, 5, 6, 10):
            self.level.addItem(f"RAID {lv}", lv)
        left.lay.addWidget(self.level)
        self.go_btn = QPushButton(tr("Detect parameters"))
        self.go_btn.setObjectName("primary")
        self.go_btn.clicked.connect(self.detect)
        left.lay.addSpacing(6)
        left.lay.addWidget(self.go_btn)
        left.lay.addWidget(_lock_note(self.theme, tr("Members are only read, never written.")))
        left.lay.addWidget(self.progress)
        cols.addWidget(left, 0, Qt.AlignmentFlag.AlignTop)

        right = QVBoxLayout()
        right.setSpacing(20)
        res = Card((0, 0, 0, 0), 0)
        head = QHBoxLayout()
        head.setContentsMargins(32, 24, 32, 18)
        head.addWidget(_h3(tr("Detected parameters")))
        head.addStretch(1)
        self.res_note = QLabel(tr("Add members and press Detect parameters."))
        self.res_note.setObjectName("muted")
        head.addWidget(self.res_note)
        res.lay.addLayout(head)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["", tr("LEVEL"), tr("CHUNK"), tr("LAYOUT"), tr("ORDER"), tr("CONFIDENCE")])
        _style_table(self.table, (64, 110, 100, 175, 110, 200))
        self.table.setItemDelegateForColumn(0, RadioDelegate(self.theme))
        self.table.setItemDelegateForColumn(5, ChanceDelegate(self.theme))
        self.table.setMinimumHeight(150)
        self.table.itemSelectionChanged.connect(self._selected)
        res.lay.addWidget(self.table)
        right.addWidget(res, 1)
        sel = Card((32, 26, 32, 26), 8)
        sel.lay.addWidget(_h3(tr("Selected result")))
        self.sel_text = QLabel(tr("Nothing detected yet."))
        self.sel_text.setObjectName("muted")
        self.sel_text.setWordWrap(True)
        self.sel_text.setStyleSheet("font-size: 16px;")
        sel.lay.addWidget(self.sel_text)
        right.addWidget(sel)
        bar = Card((32, 20, 24, 20), 0)
        brow = QHBoxLayout()
        nxt = QLabel(tr("Next, scan the rebuilt array for files."))
        nxt.setObjectName("muted")
        nxt.setStyleSheet("font-size: 16px;")
        self.use_btn = QPushButton(tr('Use the best result in "I deleted files"'))
        self.use_btn.setObjectName("primary")
        self.use_btn.setEnabled(False)
        self.use_btn.clicked.connect(self.use_selected)
        brow.addWidget(nxt, 1)
        brow.addWidget(self.use_btn)
        bar.lay.addLayout(brow)
        right.addWidget(bar)
        right.addWidget(self.status)
        cols.addLayout(right, 1)
        self.body_lay.addLayout(cols, 1)
        self.cands: list = []
        self.paths: list[str] = []

    def add_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, tr("Member images"), "", "Disk images (*.img *.dd *.raw *.vhd *.vhdx *.vmdk *.vdi);;All files (*)")
        if paths:
            self.members.appendPlainText("\n".join(paths))

    def detect(self) -> None:
        paths = [p.strip() for p in self.members.toPlainText().splitlines() if p.strip()]
        if len(paths) < 2:
            QMessageBox.information(self, NAME, tr("Add at least two members."))
            return
        lv = self.level.currentData()
        self.stepper.set_step(2)

        def job(_progress):
            mem = [open_source(p) for p in paths]
            try:
                return paths, raid.detect(mem, levels=None if lv is None else (lv,))
            finally:
                for m in mem:
                    m.close()

        def done(res) -> None:
            self.paths, self.cands = res
            self.table.setRowCount(0)
            if not self.cands:
                self.res_note.setText(tr("No layout was recognised."))
                self.sel_text.setText(tr("No layout produced a recognisable partition table or filesystem. Check that all members are present."))
                self.use_btn.setEnabled(False)
                return
            best = max(c.score for c in self.cands) or 1
            for c in self.cands:
                r = self.table.rowCount()
                self.table.insertRow(r)
                pct = max(1.0, min(100.0, 100.0 * c.score / best))
                level = "high" if pct >= 85 else "medium" if pct >= 40 else "low"
                conf = QTableWidgetItem("")
                conf.setData(CHANCE_ROLE, (pct, f"{tr(level.capitalize())} · {pct:.0f}%", level))
                for col, text in ((0, ""), (1, f"RAID {c.level}"), (2, f"{c.chunk // 1024} KB" if c.level != 1 else "-"), (3, layout_name(c)),
                                  (4, ", ".join(str(i + 1) for i in c.order))):
                    it = QTableWidgetItem(text)
                    if col == 1:
                        f = it.font()
                        f.setBold(True)
                        it.setFont(f)
                    self.table.setItem(r, col, it)
                self.table.setItem(r, 5, conf)
            self.table.selectRow(0)
            self.res_note.setText(tr("{n} candidate(s), best first").format(n=len(self.cands)))
            self.stepper.set_step(3)

        self.run_bg(job, done, tr("Trying chunk sizes, layouts and member orders... (cancel any time)"))

    def _selected(self) -> None:
        r = self.table.currentRow()
        if r < 0 or r >= len(self.cands):
            return
        c = self.cands[r]
        n = len(self.paths)
        self.sel_text.setText(tr("RAID {lv} across {n} members with a {k} KB chunk.").format(lv=c.level, n=n, k=c.chunk // 1024) + " " + tr(REDUNDANCY.get(c.level, "")))
        self.use_btn.setEnabled(True)

    def use_selected(self) -> None:
        r = self.table.currentRow()
        if r < 0 or r >= len(self.cands):
            return
        c = self.cands[r]
        spec = raid.spec_for(c.level, c.chunk, c.layout, [self.paths[i] for i in c.order])
        page = self.win.pages["recover"]
        page.src.edit.setText(spec)  # type: ignore[attr-defined]
        self.win.show_page("recover")


_ = (QFrame, json, QVBoxLayout)
