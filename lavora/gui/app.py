"""PySide6 GUI shell: wizard-style home page plus working Recover, Doctor, Image, Backup and Boot-repair pages.

Everything heavy runs in a worker thread and calls the same core functions as the CLI. Read-only by default;
anything that writes asks for the same typed confirmations the CLI does.
"""
from __future__ import annotations

import os
import sys
import traceback
from collections.abc import Callable

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..brand import NAME, TAGLINE
from ..core import backup, boot, disks, doctor, imaging, license, safety
from ..core import partitions as parts
from ..core.device import BlockSource, PartitionView, open_source
from ..core.fs import FsError, open_filesystem
from ..core.recovery import filter_entries, preview, recover
from ..i18n import tr
from . import theme


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
    def __init__(self, win: MainWindow):
        super().__init__()
        self.win = win
        self._threads: list[QThread] = []
        self._workers: list[Worker] = []
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)

    def run_bg(self, fn, on_done, label: str = "") -> None:
        t = QThread(self)
        w = Worker(fn)
        w.moveToThread(t)
        t.started.connect(w.run)

        def on_progress(d: int, tot: int) -> None:
            self.progress.setMaximum(max(tot, 1))
            self.progress.setValue(d)

        def finished_ok(result: object) -> None:
            self.progress.setVisible(False)
            on_done(result)
            t.quit()

        def finished_bad(msg: str) -> None:
            self.progress.setVisible(False)
            self.status.setText(msg)
            QMessageBox.warning(self, NAME, msg)
            t.quit()

        def cleanup() -> None:
            if t in self._threads:
                self._threads.remove(t)

        w.progress.connect(on_progress)
        w.done.connect(finished_ok)
        w.failed.connect(finished_bad)
        t.finished.connect(cleanup)
        self._threads.append(t)
        self._workers.append(w)
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self.status.setText(label)
        t.start()

    def header(self, title: str, subtitle: str = "") -> QVBoxLayout:
        lay = QVBoxLayout(self)
        back = QPushButton("< " + tr("Home"))
        back.clicked.connect(lambda: self.win.show_page("home"))
        back.setFixedWidth(110)
        t = QLabel(title)
        t.setObjectName("title")
        lay.addWidget(back)
        lay.addWidget(t)
        if subtitle:
            s = QLabel(subtitle)
            s.setObjectName("muted")
            s.setWordWrap(True)
            lay.addWidget(s)
        return lay


def pick_source(parent: QWidget, edit: QLineEdit) -> None:
    path, _ = QFileDialog.getOpenFileName(parent, tr("Choose a disk image"), "", "Disk images (*.img *.dd *.raw *.vhd *.iso);;All files (*)")
    if path:
        edit.setText(path)


class SourceRow(QWidget):
    """Choose an image file or a physical disk from the list."""

    def __init__(self):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(tr("Image file (.img/.vhd) or \\\\.\\PhysicalDriveN"))
        browse = QPushButton(tr("Browse image..."))
        browse.clicked.connect(lambda: pick_source(self, self.edit))
        lst = QPushButton(tr("Detect disks"))
        lst.clicked.connect(self.detect)
        self.combo = QComboBox()
        self.combo.setVisible(False)
        self.combo.activated.connect(lambda _i: self.edit.setText(self.combo.currentData() or ""))
        lay.addWidget(self.edit, 1)
        lay.addWidget(browse)
        lay.addWidget(lst)
        lay.addWidget(self.combo)

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


class RecoverPage(Page):
    def __init__(self, win: MainWindow):
        super().__init__(win)
        lay = self.header(tr("I deleted files"), tr("Scan a disk or image for deleted files, preview them, and recover them to a DIFFERENT drive. Nothing is written to the source."))
        self.src = SourceRow()
        self.part = QComboBox()
        self.part.addItem(tr("Auto-detect volume"), None)
        self.volumes_btn = QPushButton(tr("List volumes"))
        self.volumes_btn.clicked.connect(self.list_volumes)
        row = QHBoxLayout()
        row.addWidget(self.part, 1)
        row.addWidget(self.volumes_btn)
        self.scan_btn = QPushButton(tr("Scan for deleted files"))
        self.scan_btn.setObjectName("primary")
        self.scan_btn.clicked.connect(self.scan)
        self.carve_btn = QPushButton(tr("Deep scan (by file type)"))
        self.carve_btn.clicked.connect(self.carve)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText(tr("Filter by name or type, e.g. jpg"))
        self.filter.textChanged.connect(self.apply_filter)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels([tr("Name"), tr("Size"), tr("State"), tr("Chance"), tr("Path")])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self.show_preview)
        self.prev = QPlainTextEdit()
        self.prev.setReadOnly(True)
        self.prev.setMaximumHeight(130)
        self.prev_img = QLabel()
        self.prev_img.setMaximumHeight(130)
        self.recover_btn = QPushButton(tr("Recover selected to..."))
        self.recover_btn.setObjectName("primary")
        self.recover_btn.clicked.connect(self.do_recover)
        btns = QHBoxLayout()
        btns.addWidget(self.scan_btn)
        btns.addWidget(self.carve_btn)
        for w in (self.src,):
            lay.addWidget(w)
        lay.addLayout(row)
        lay.addLayout(btns)
        lay.addWidget(self.filter)
        lay.addWidget(self.table, 1)
        pv = QHBoxLayout()
        pv.addWidget(self.prev, 1)
        pv.addWidget(self.prev_img)
        lay.addLayout(pv)
        lay.addWidget(self.recover_btn)
        lay.addWidget(self.progress)
        lay.addWidget(self.status)
        self.entries: list = []
        self.fs = None
        self.vol: BlockSource | None = None
        self.carved: list = []

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

    def scan(self) -> None:
        def job(progress):
            vol = self._volume()
            fs = open_filesystem(vol)
            return vol, fs, list(fs.scan(progress=progress))
        self.run_bg(job, self.scanned, tr("Reading the file table..."))

    def scanned(self, res) -> None:
        self.vol, self.fs, self.entries = res
        self.carved = []
        deleted = sum(e.deleted for e in self.entries)
        self.status.setText(tr("{n} files, {d} deleted.").format(n=len(self.entries), d=deleted))
        self.apply_filter()

    def carve(self) -> None:
        from ..core import carver

        def job(progress):
            src = open_source(self.src.path)
            return src, list(carver.carve(src, progress=progress))
        self.run_bg(job, self.carved_done, tr("Scanning for file signatures..."))

    def carved_done(self, res) -> None:
        self.vol, self.carved = res
        self.entries, self.fs = [], None
        self.table.setRowCount(0)
        for c in self.carved:
            r = self.table.rowCount()
            self.table.insertRow(r)
            for i, v in enumerate([c.name, str(c.size), tr("carved"), "?", f"offset {c.offset}"]):
                self.table.setItem(r, i, QTableWidgetItem(v))
        self.status.setText(tr("{n} files found by signature.").format(n=len(self.carved)))

    def apply_filter(self) -> None:
        if self.carved:
            return
        shown = filter_entries(self.entries, name_contains=self.filter.text().strip())
        self.shown = shown
        self.table.setRowCount(0)
        for e in shown[:5000]:
            r = self.table.rowCount()
            self.table.insertRow(r)
            chance = "-" if not e.deleted else (tr("lost") if e.overwritten_pct >= 100 else tr("good") if e.overwritten_pct == 0 else f"{100 - e.overwritten_pct:.0f}%")
            for i, v in enumerate([e.name, str(e.size), tr("deleted") if e.deleted else tr("present"), chance, e.path]):
                self.table.setItem(r, i, QTableWidgetItem(v))

    def show_preview(self) -> None:
        rows = {i.row() for i in self.table.selectedItems()}
        if len(rows) != 1 or not self.fs or not hasattr(self, "shown"):
            return
        e = self.shown[next(iter(rows))]
        try:
            p = preview(self.fs, e)
        except Exception as ex:  # noqa: BLE001
            self.prev.setPlainText(str(ex))
            return
        self.prev_img.clear()
        if p["kind"] == "image":
            pm = QPixmap()
            if pm.loadFromData(p["data"]):
                self.prev_img.setPixmap(pm.scaledToHeight(120, Qt.SmoothTransformation))
            self.prev.setPlainText(tr("Image preview"))
        else:
            self.prev.setPlainText(p["data"])

    def do_recover(self) -> None:
        out = QFileDialog.getExistingDirectory(self, tr("Recover to (choose a different drive)"))
        if not out or not self.vol:
            return
        rows = sorted({i.row() for i in self.table.selectedItems()})
        if not rows:
            QMessageBox.information(self, NAME, tr("Select the files to recover first."))
            return
        lic = license.current()

        def job(progress):
            if self.carved:
                from ..core import carver
                safety.check_output_location(self.vol, out)
                return [carver.extract(self.vol, self.carved[r], out) for r in rows]
            return recover(self.fs, [self.shown[r] for r in rows], out, source=self.vol, license=lic, progress=progress)
        self.run_bg(job, lambda rep: self.status.setText(
            tr("Recovered {n} file(s) to {p}").format(n=len(rep) if isinstance(rep, list) else rep.recovered, p=out)), tr("Recovering..."))


class DoctorPage(Page):
    def __init__(self, win: MainWindow):
        super().__init__(win)
        lay = self.header(tr("PC won't boot - Lavora Doctor"), tr("Scans partition tables, boot sectors and boot files, then proposes ranked fixes. Every fix takes an undo snapshot first."))
        self.src = SourceRow()
        self.run_btn = QPushButton(tr("Diagnose"))
        self.run_btn.setObjectName("primary")
        self.run_btn.clicked.connect(self.diagnose)
        self.list = QTableWidget(0, 3)
        self.list.setHorizontalHeaderLabels([tr("Severity"), tr("Problem"), tr("Fix")])
        self.list.horizontalHeader().setStretchLastSection(True)
        self.dry = QCheckBox(tr("Dry run (show what would change, write nothing)"))
        self.dry.setChecked(True)
        self.apply_btn = QPushButton(tr("Apply selected fix"))
        self.apply_btn.clicked.connect(self.apply)
        self.undo_btn = QPushButton(tr("Undo last fix"))
        self.undo_btn.clicked.connect(self.undo_last)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        lay.addWidget(self.src)
        lay.addWidget(self.run_btn)
        lay.addWidget(self.list, 1)
        lay.addWidget(self.dry)
        row = QHBoxLayout()
        row.addWidget(self.apply_btn)
        row.addWidget(self.undo_btn)
        lay.addLayout(row)
        lay.addWidget(self.log)
        lay.addWidget(self.progress)
        lay.addWidget(self.status)
        self.rows: list = []
        self.last_undo: str | None = None

    def diagnose(self) -> None:
        try:
            license.current().require("doctor")
        except license.LicenseRequired as ex:
            QMessageBox.information(self, NAME, str(ex))
            return

        def job(_p):
            with open_source(self.src.path) as s:
                return doctor.diagnose(s)
        self.run_bg(job, self.show_findings, tr("Diagnosing..."))

    def show_findings(self, findings) -> None:
        self.rows = [(f, fx) for f in findings for fx in (f.fixes or [None])]
        self.list.setRowCount(0)
        for f, fx in self.rows:
            r = self.list.rowCount()
            self.list.insertRow(r)
            for i, v in enumerate([f.severity.upper(), f"{f.title}: {f.detail}", (fx.title if fx else "-") + ("" if not fx or fx.auto else " (manual plan)")]):
                self.list.setItem(r, i, QTableWidgetItem(v))
        self.status.setText(tr("No problems found.") if not findings else tr("{n} finding(s).").format(n=len(findings)))

    def apply(self) -> None:
        r = self.list.currentRow()
        if r < 0 or self.rows[r][1] is None:
            return
        fx = self.rows[r][1]
        if not fx.auto:
            self.log.setPlainText(tr("This is a plan, not an automatic fix. Run these yourself:") + "\n" + "\n".join(fx.commands))
            return
        if not self.dry.isChecked() and self.src.path.lower().startswith("\\\\.\\physicaldrive"):
            text, ok = QInputDialog.getText(self, NAME, tr('This writes to a physical disk. Type "APPLY {id}" to continue.').format(id=fx.id))
            if not ok or text.strip().lower() != f"apply {fx.id}".lower():
                return
        res = doctor.apply_fix(self.src.path, fx, dry_run=self.dry.isChecked())
        if res.applied:
            self.last_undo = res.undo_id
            self.log.setPlainText(tr("Applied. Undo id: {u}").format(u=res.undo_id))
        else:
            self.log.setPlainText(tr("Dry run: would write ") + ", ".join(f"{n} bytes at {o}" for o, n in res.plan))

    def undo_last(self) -> None:
        from ..core import undo
        snaps = undo.list_snapshots()
        if not snaps:
            self.log.setPlainText(tr("Nothing to undo."))
            return
        target = undo.undo(snaps[-1]["id"])
        self.log.setPlainText(tr("Restored {t} from snapshot {s}").format(t=target, s=snaps[-1]["id"]))


class ImagePage(Page):
    def __init__(self, win: MainWindow):
        super().__init__(win)
        lay = self.header(tr("Disk is failing"), tr("Make a sector-by-sector image first (bad sectors are mapped and retried), then scan and recover from the image."))
        self.src = SourceRow()
        self.dest = QLineEdit()
        self.dest.setPlaceholderText(tr("Where to save the image (on a DIFFERENT drive)"))
        b = QPushButton(tr("Choose..."))
        b.clicked.connect(lambda: self.dest.setText(QFileDialog.getSaveFileName(self, tr("Image file"), "", "Disk image (*.img)")[0]))
        go = QPushButton(tr("Create image"))
        go.setObjectName("primary")
        go.clicked.connect(self.go)
        row = QHBoxLayout()
        row.addWidget(self.dest, 1)
        row.addWidget(b)
        lay.addWidget(self.src)
        lay.addLayout(row)
        lay.addWidget(go)
        lay.addWidget(self.progress)
        lay.addWidget(self.status)
        lay.addStretch(1)

    def go(self) -> None:
        try:
            license.current().require("imaging")
        except license.LicenseRequired as ex:
            QMessageBox.information(self, NAME, str(ex))
            return
        dest = self.dest.text().strip()
        if not dest:
            return

        def job(progress):
            with open_source(self.src.path) as s:
                return imaging.ImageJob(s, dest).run(progress)
        self.run_bg(job, lambda r: self.status.setText(tr("Image {p}: complete={c}, bad bytes={b}").format(p=r.path, c=r.complete, b=r.bad_bytes)),
                    tr("Imaging..."))


class BackupPage(Page):
    def __init__(self, win: MainWindow):
        super().__init__(win)
        lay = self.header(tr("Back up my PC"), tr("Incremental, compressed, optionally AES-256 encrypted file backups with a test-restore check."))
        self.folder = QLineEdit()
        self.folder.setPlaceholderText(tr("Folder to back up"))
        fb = QPushButton(tr("Choose..."))
        fb.clicked.connect(lambda: self.folder.setText(QFileDialog.getExistingDirectory(self, tr("Folder to back up"))))
        self.dest = QLineEdit()
        self.dest.setPlaceholderText(tr("Backup destination (external drive or network share)"))
        db = QPushButton(tr("Choose..."))
        db.clicked.connect(lambda: self.dest.setText(QFileDialog.getExistingDirectory(self, tr("Destination"))))
        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.EchoMode.Password)
        self.pw.setPlaceholderText(tr("Password (leave empty for no encryption)"))
        go = QPushButton(tr("Back up and verify"))
        go.setObjectName("primary")
        go.clicked.connect(self.go)
        for a, b in ((self.folder, fb), (self.dest, db)):
            r = QHBoxLayout()
            r.addWidget(a, 1)
            r.addWidget(b)
            lay.addLayout(r)
        lay.addWidget(self.pw)
        lay.addWidget(go)
        lay.addWidget(self.progress)
        lay.addWidget(self.status)
        lay.addStretch(1)

    def go(self) -> None:
        try:
            license.current().require("backup")
        except license.LicenseRequired as ex:
            QMessageBox.information(self, NAME, str(ex))
            return
        folder, dest, pw = self.folder.text().strip(), self.dest.text().strip(), self.pw.text() or None

        def job(_p):
            try:
                repo = backup.Repository.open(dest, pw)
            except backup.BackupError:
                repo = backup.Repository.init(dest, pw)
            st = backup.backup(repo, [folder])
            return st, backup.verify_backup(repo, st.snapshot_id)
        self.run_bg(job, lambda r: self.status.setText(
            tr("Backed up {n} files. Test restore: {v}").format(n=r[0].files, v=tr("OK") if r[1].ok else tr("FAILED"))), tr("Backing up..."))


class BootPage(Page):
    def __init__(self, win: MainWindow):
        super().__init__(win)
        lay = self.header(tr("Boot repair plans"), tr("Exact commands for Windows (BCD, SFC/DISM, registry) and Linux (GRUB, initramfs, packages, fsck). Shown for you to review; nothing runs from here."))
        self.kind = QComboBox()
        self.kinds = {
            tr("Windows: rebuild BCD (UEFI)"): lambda: boot.windows_bcd_rebuild("C:", "S:", True),
            tr("Windows: rebuild BCD (BIOS)"): lambda: boot.windows_bcd_rebuild("C:", "S:", False),
            tr("Windows: offline SFC / DISM"): lambda: boot.windows_offline_sfc_dism("C:"),
            tr("Windows: restore registry hives"): lambda: boot.windows_registry_restore("C:"),
            tr("Linux: repair GRUB (UEFI)"): lambda: boot.linux_grub_repair("/dev/sda2", "/dev/sda", "/dev/sda1"),
            tr("Linux: repair GRUB (BIOS)"): lambda: boot.linux_grub_repair("/dev/sda1", "/dev/sda"),
            tr("Linux: regenerate initramfs"): lambda: boot.linux_initramfs("/dev/sda2"),
            tr("Linux: fix broken packages"): lambda: boot.linux_package_repair("/dev/sda2"),
        }
        self.kind.addItems(list(self.kinds))
        self.kind.currentTextChanged.connect(self.show_plan)
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        lay.addWidget(self.kind)
        lay.addWidget(self.out, 1)
        self.show_plan()

    def show_plan(self) -> None:
        self.out.setPlainText(self.kinds[self.kind.currentText()]().as_text())


class PasswordPage(Page):
    def __init__(self, win: MainWindow):
        super().__init__(win)
        lay = self.header(tr("Forgot password"), tr("Owner-only. You must confirm that this is your own device. Microsoft and domain accounts and encrypted volumes are never bypassed. Every attempt is written to an audit log."))
        msg = QLabel(tr("Linux: reset a user's password inside a mounted root with `lavora-cli account linux <mounted-root> <user> --attest \"this is my device\" --apply` (from Lavora Rescue).\n\nWindows local-account reset is not available in this version. For a Microsoft account use account.live.com/password/reset."))
        msg.setWordWrap(True)
        lay.addWidget(msg)
        lay.addStretch(1)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{NAME} {__version__}")
        self.resize(1000, 720)
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self.pages: dict[str, QWidget] = {"home": self._home()}
        for key, cls in (("recover", RecoverPage), ("doctor", DoctorPage), ("image", ImagePage), ("backup", BackupPage),
                         ("boot", BootPage), ("password", PasswordPage)):
            self.pages[key] = cls(self)
        for p in self.pages.values():
            self.stack.addWidget(p)
        self.show_page("home")

    def show_page(self, key: str) -> None:
        self.stack.setCurrentWidget(self.pages[key])

    def _home(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        t = QLabel(NAME)
        t.setObjectName("title")
        s = QLabel(tr(TAGLINE.split(" - ", 1)[1].capitalize()))
        s.setObjectName("muted")
        lic = license.current()
        tier = QLabel(tr("Pro licence") if lic.is_pro else tr("Free tier - scan, preview, recover up to 256 MiB, basic repair"))
        tier.setObjectName("muted")
        lay.addWidget(t)
        lay.addWidget(s)
        lay.addWidget(tier)
        grid = QGridLayout()
        tiles = [("boot", tr("PC won't boot\nDiagnose and repair boot problems")), ("recover", tr("I deleted files\nScan, preview and recover")),
                 ("password", tr("Forgot password\nOwner-only account access")), ("image", tr("Disk is failing\nImage it first, recover from the image")),
                 ("backup", tr("Back up my PC\nIncremental, encrypted, verified"))]
        for i, (key, text) in enumerate(tiles):
            b = QPushButton(text)
            b.setObjectName("tile")
            target = "doctor" if key == "boot" else key
            b.clicked.connect(lambda _c=False, k=target: self.show_page(k))
            grid.addWidget(b, i // 2, i % 2)
        b = QPushButton(tr("Boot repair plans\nWindows and Linux commands"))
        b.setObjectName("tile")
        b.clicked.connect(lambda: self.show_page("boot"))
        grid.addWidget(b, 2, 1)
        lay.addLayout(grid)
        lay.addStretch(1)
        note = QLabel(tr("Read-only by default. Recovery always goes to a different drive. No telemetry."))
        note.setObjectName("muted")
        lay.addWidget(note)
        return w


def detect_theme(app: QApplication) -> str:
    try:
        return "dark" if app.styleHints().colorScheme() == Qt.ColorScheme.Dark else "light"
    except AttributeError:
        return "light"


def build_window(app: QApplication) -> MainWindow:
    app.setStyleSheet(theme.stylesheet(detect_theme(app)))
    return MainWindow()


def run() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(NAME)
    win = build_window(app)  # type: ignore[arg-type]
    win.show()
    return app.exec()


if __name__ == "__main__":
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")
    raise SystemExit(run())
