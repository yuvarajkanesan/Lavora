"""'Check disk health': a read-only surface scan with speed, slow and unreadable areas, and SMART where available."""
from __future__ import annotations

from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout

from ..brand import NAME
from ..core import health
from ..core.device import open_source
from ..i18n import tr
from .base import Page, SourceRow
from .parts import Notice, NumberedList, Tag
from .recover_page import fmt_size
from .widgets import Card


def _label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("fieldLabel")
    return lbl


class HealthPage(Page):
    SIZES = ((tr("Quick: first 1 GB"), 1 << 30), (tr("First 20 GB"), 20 << 30), (tr("First 200 GB"), 200 << 30), (tr("Whole disk (slow)"), 0))

    def __init__(self, win):
        super().__init__(win)
        self.header(tr("Check disk health"), tr("A read-only test that reads the disk and times every block. It finds unreadable and slow areas early, before data is lost."), card=False)
        cols = QHBoxLayout()
        cols.setSpacing(28)
        form = Card((34, 26, 34, 26), 10)
        form.lay.addWidget(_label(tr("Disk or image")))
        self.src = SourceRow(inline=True)
        form.lay.addWidget(self.src)
        form.lay.addWidget(_label(tr("How much to test")))
        self.amount = QComboBox()
        for label, n in self.SIZES:
            self.amount.addItem(label, n)
        form.lay.addWidget(self.amount)
        self.go_btn = QPushButton(tr("Start health check"))
        self.go_btn.setObjectName("primary")
        self.go_btn.clicked.connect(self.start)
        form.lay.addSpacing(4)
        form.lay.addWidget(self.go_btn)
        form.lay.addWidget(self.progress)
        form.lay.addWidget(self.status)
        cols.addWidget(form, 3)

        res = Card((34, 26, 34, 26), 12)
        head = QHBoxLayout()
        h = QLabel(tr("Result"))
        h.setStyleSheet("font-size: 19px; font-weight: 700;")
        head.addWidget(h)
        head.addStretch(1)
        self.verdict = Tag(tr("Not tested"), "neutral", self.theme)
        head.addWidget(self.verdict)
        res.lay.addLayout(head)
        self.stats: dict[str, QLabel] = {}
        row = QHBoxLayout()
        row.setSpacing(30)
        for key, name in (("scanned", tr("Tested")), ("speed", tr("Average speed")), ("bad", tr("Unreadable blocks")), ("slow", tr("Slow blocks"))):
            col = QLabel(name)
            col.setObjectName("muted")
            val = QLabel("—")
            val.setStyleSheet("font-size: 17px; font-weight: 700;")
            self.stats[key] = val
            v = QVBoxLayout()
            v.setSpacing(4)
            v.addWidget(col)
            v.addWidget(val)
            row.addLayout(v)
        row.addStretch(1)
        res.lay.addLayout(row)
        self.advice = Notice(tr("Run the test to see the result."), "info", "info-circle", self.theme)
        res.lay.addWidget(self.advice)
        self.smart_lbl = QLabel("")
        self.smart_lbl.setWordWrap(True)
        self.smart_lbl.setObjectName("muted")
        res.lay.addWidget(self.smart_lbl)
        self.image_btn = QPushButton(tr("Image this disk now"))
        self.image_btn.setVisible(False)
        self.image_btn.clicked.connect(self._to_image)
        res.lay.addWidget(self.image_btn)
        res.lay.addWidget(NumberedList([
            "<b>" + tr("Read-only.") + "</b> " + tr("Nothing is ever written to the disk."),
            "<b>" + tr("Blocks that take over half a second") + "</b> " + tr("are reported as slow: a common early sign of a failing drive."),
            "<b>" + tr("SMART") + "</b> " + tr("is shown when the drive or its USB bridge reports it (needs smartmontools)."),
        ], self.theme, size=14))
        res.lay.addStretch(1)
        cols.addWidget(res, 4)
        self.body_lay.addLayout(cols, 1)
        self._report: health.HealthReport | None = None

    def start(self) -> None:
        path = self.src.path
        if not path:
            QMessageBox.information(self, NAME, tr("Choose the disk or image to test first."))
            return
        limit = self.amount.currentData() or None
        is_disk = path.lower().startswith(("\\\\.\\physicaldrive", "/dev/"))
        self.image_btn.setVisible(False)

        def job(progress):
            with open_source(path) as src:
                return health.check_health(src, max_bytes=limit, smart_device=path if is_disk else None, progress=progress, cancel=self.token)

        def done(rep: health.HealthReport) -> None:
            self._report = rep
            self.stats["scanned"].setText(fmt_size(rep.scanned))
            self.stats["speed"].setText(f"{rep.speed / 1e6:.1f} MB/s")
            self.stats["bad"].setText(str(len(rep.unreadable)))
            self.stats["slow"].setText(str(len(rep.slow)))
            tone = {"healthy": "ok", "slow": "warn", "failing": "danger"}[rep.verdict]
            self.verdict.set_tone(tone, {"healthy": tr("Healthy"), "slow": tr("Slow areas"), "failing": tr("Failing")}[rep.verdict])
            self.advice.label.setText(tr(rep.advice()) + (" " + tr("(test stopped early)") if rep.cancelled else ""))
            self.smart_lbl.setText(("SMART: " + rep.smart.status + (" - " + "; ".join(rep.smart.warnings) if rep.smart.warnings else "")) if rep.smart and rep.smart.available
                                   else (rep.smart.detail if rep.smart else ""))
            self.image_btn.setVisible(rep.verdict != "healthy")
        self.run_bg(job, done, tr("Testing... (you can cancel)"))

    def _to_image(self) -> None:
        page = self.win.pages["image"]
        page.src.edit.setText(self.src.path)  # type: ignore[attr-defined]
        self.win.show_page("image")
