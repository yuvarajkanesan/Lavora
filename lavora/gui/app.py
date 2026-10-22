"""PySide6 GUI shell: wizard-style home page plus working Recover, Doctor, Image, Backup and Boot-repair pages.

Everything heavy runs in a worker thread and calls the same core functions as the CLI. Read-only by default;
anything that writes asks for the same typed confirmations the CLI does.
"""
from __future__ import annotations

import os
import sys
import tempfile

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..brand import NAME
from ..core import boot, license
from ..core.device import open_source
from ..i18n import tr
from . import icons, theme
from .base import Page, SourceRow, Worker  # noqa: F401 - Worker re-exported for callers
from .home import HomePage
from .pages import BackupPage, DoctorPage, ImagePage, PasswordPage, RaidPage
from .recover_page import RecoverPage
from .widgets import TopBar


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


class EditorPage(Page):
    """Read-only hex viewer with a structure decoder and search. Writing sectors is a CLI feature (sector-write), on purpose."""

    VIEW = 1024

    def __init__(self, win: MainWindow):
        super().__init__(win)
        lay = self.header(tr("Disk editor"), tr("Look at any sector of a disk, image, virtual disk or RAID. Viewing never writes; to change sectors use the CLI sector-write (dry run, undo snapshot)."))
        self.src = SourceRow()
        self.pos_edit = QLineEdit("0")
        self.pos_edit.setPlaceholderText(tr("Offset in bytes (0x... for hex)"))
        go = QPushButton(tr("Go"))
        go.clicked.connect(self.show_at)
        prev_b, next_b = QPushButton("<"), QPushButton(">")
        prev_b.clicked.connect(lambda: self.step(-self.VIEW))
        next_b.clicked.connect(lambda: self.step(self.VIEW))
        row = QHBoxLayout()
        for w in (self.pos_edit, go, prev_b, next_b):
            row.addWidget(w)
        self.find_text = QLineEdit()
        self.find_text.setPlaceholderText(tr("Search text (or hex: 0x4e544653)"))
        find_b = QPushButton(tr("Find next"))
        find_b.clicked.connect(self.find_next)
        frow = QHBoxLayout()
        frow.addWidget(self.find_text, 1)
        frow.addWidget(find_b)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setStyleSheet("font-family: Consolas, 'Cascadia Mono', monospace;")
        self.info = QPlainTextEdit()
        self.info.setReadOnly(True)
        self.info.setMaximumHeight(120)
        lay.addWidget(self.src)
        lay.addLayout(row)
        lay.addLayout(frow)
        lay.addWidget(self.view, 1)
        lay.addWidget(self.info)
        lay.addWidget(self.progress)
        lay.addWidget(self.status)
        self.offset = 0

    def _offset(self) -> int:
        return int(self.pos_edit.text().strip() or "0", 0)

    def show_at(self, offset: int | None = None) -> None:
        from ..core import diskedit
        try:
            self.offset = max(0, self._offset() if offset is None else offset)
            self.offset -= self.offset % 16
            with open_source(self.src.path) as s:
                self.view.setPlainText(diskedit.hexview(s, self.offset, self.VIEW))
                import json
                self.info.setPlainText(json.dumps(diskedit.describe(s.read_at(self.offset - self.offset % 512, 512)), indent=2))
                self.status.setText(tr("Size: {n} bytes").format(n=s.size))
            self.pos_edit.setText(f"0x{self.offset:x}")
        except Exception as ex:  # noqa: BLE001
            QMessageBox.warning(self, NAME, str(ex))

    def step(self, delta: int) -> None:
        self.show_at(self.offset + delta)

    def find_next(self) -> None:
        from ..core import diskedit
        txt = self.find_text.text().strip()
        if not txt:
            return
        pattern = bytes.fromhex(txt[2:]) if txt.lower().startswith("0x") else txt.encode()
        start = self.offset + 1

        def job(progress):
            with open_source(self.src.path) as s:
                return next(iter(diskedit.find(s, pattern, start=start, cancel=self.token, progress=progress, limit=1)), None)

        def done(off) -> None:
            if off is None:
                self.status.setText(tr("Not found (to the end of the source)") + (" " + tr("[stopped]") if self.token.cancelled else ""))
            else:
                self.show_at(off)

        self.run_bg(job, done, tr("Searching... (cancel any time)"))


class MainWindow(QMainWindow):
    def __init__(self, theme_name: str = "light") -> None:
        super().__init__()
        self.theme_name = theme_name
        self.setWindowTitle(f"{NAME} {__version__}")
        self.resize(1360, 900)
        self.setMinimumSize(980, 640)
        root = QWidget()
        root.setObjectName("root")
        col = QVBoxLayout(root)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        lic = license.current()
        self.topbar = TopBar(__version__, lic.is_pro, theme_name, tr("Pro licence") if lic.is_pro else tr("Free tier"), tr("Read-only mode"))
        col.addWidget(self.topbar)
        self.stack = QStackedWidget()
        col.addWidget(self.stack, 1)
        self.setCentralWidget(root)
        self.pages: dict[str, QWidget] = {"home": HomePage(self)}
        for key, cls in (("recover", RecoverPage), ("doctor", DoctorPage), ("image", ImagePage), ("backup", BackupPage),
                         ("boot", BootPage), ("password", PasswordPage), ("editor", EditorPage), ("raid", RaidPage)):
            self.pages[key] = cls(self)
        for page in self.pages.values():
            self.stack.addWidget(page)
        self._pin_control_heights()
        self.show_page("home")

    def _pin_control_heights(self) -> None:
        """Style-sheet min-height is not part of a widget's minimum size hint, so layouts could squeeze buttons and inputs.
        Pin the heights in code so every control keeps the size the design calls for."""
        heights = {"primary": 52, "chipBtn": 44, "link": 28, "crumb": 28}
        for b in self.findChildren(QPushButton):
            b.setMinimumHeight(heights.get(b.objectName(), 46))
        for edit in self.findChildren(QLineEdit):
            edit.setMinimumHeight(48)
        for combo in self.findChildren(QComboBox):
            combo.setMinimumHeight(48)

    def show_page(self, key: str) -> None:
        self.stack.setCurrentWidget(self.pages[key])

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        """Stop background work and let worker threads finish before Qt tears everything down (avoids a crash on exit)."""
        for page in self.pages.values():
            token = getattr(page, "token", None)
            if token is not None:
                token.cancel()
            for t in list(getattr(page, "_threads", [])):
                t.quit()
                t.wait(3000)
        super().closeEvent(event)


def detect_theme(app: QApplication) -> str:
    try:
        return "dark" if app.styleHints().colorScheme() == Qt.ColorScheme.Dark else "light"
    except AttributeError:
        return "light"


def _asset_pngs(muted: str) -> tuple[str, str]:
    """Check tick and dropdown chevron as image files (Qt style sheets can only reference files)."""
    tmp = tempfile.gettempdir()
    check, arrow = os.path.join(tmp, "lavora-check.png"), os.path.join(tmp, "lavora-chevron-down.png")
    icons.glyph_pixmap("check", "#ffffff", 20, 12).save(check, "PNG")
    icons.glyph_pixmap("chevron-down", muted, 14, 12).save(arrow, "PNG")
    return check.replace("\\", "/"), arrow.replace("\\", "/")


def build_window(app: QApplication) -> MainWindow:
    name = detect_theme(app)
    check, arrow = _asset_pngs(theme.PALETTES[name]["muted"])
    app.setStyleSheet(theme.stylesheet(name, check, arrow))
    return MainWindow(name)


def run() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(NAME)
    win = build_window(app)  # type: ignore[arg-type]
    win.showMaximized()  # opens maximized by default
    return app.exec()


if __name__ == "__main__":
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")
    raise SystemExit(run())
