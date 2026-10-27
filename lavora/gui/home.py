"""Home screen: hero heading, trust chips, and the task cards grouped as Recover data / Repair a system / Protect."""
from __future__ import annotations

from functools import partial

from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel

from ..i18n import tr
from .base import Page
from .widgets import ActionCard, FeatureChip

SECTIONS = [
    ("RECOVER DATA", [("auto", "Recover everything", "One click: finds the volumes and recovers all files", True),
                      ("recover", "I deleted files", "Scan, preview and pick files yourself", False),
                      ("image", "Disk is failing", "Image it first, recover from the image", False),
                      ("raid", "RAID builder", "Rebuild an array from its disks", False)]),
    ("REPAIR A SYSTEM", [("boot", "PC won't boot", "Diagnose and repair boot problems", False),
                         ("plans", "Boot repair plans", "Windows and Linux commands", False),
                         ("password", "Forgot password", "Owner-only account access", False)]),
    ("PROTECT", [("backup", "Back up my PC", "Incremental, encrypted, verified", False),
                 ("health", "Check disk health", "Read-only speed and error test", False),
                 ("editor", "Disk editor", "Hex view, search, decode", False)]),
]
TARGET = {"boot": "doctor", "plans": "boot"}  # card id -> page key


class HomePage(Page):
    def __init__(self, win):
        super().__init__(win)
        lay = self.body_lay
        lay.setSpacing(18)
        hero = QLabel(tr("Get your system working again."))
        hero.setObjectName("hero")
        hero.setWordWrap(True)
        lead = QLabel(tr("Choose what has gone wrong and Lavora walks you through it. Source disks are opened read-only, and recovered files always go to a different drive."))
        lead.setObjectName("lead")
        lead.setWordWrap(True)
        lead.setMaximumWidth(1000)
        lay.addSpacing(18)
        lay.addWidget(hero)
        lay.addWidget(lead)
        chips = QHBoxLayout()
        chips.setSpacing(14)
        for icon, text in (("lock", tr("Read-only by default")), ("drive", tr("Recovery goes to a different drive")), ("eye-off", tr("No telemetry"))):
            chips.addWidget(FeatureChip(icon, text, self.theme))
        chips.addStretch(1)
        lay.addLayout(chips)
        lay.addSpacing(14)
        for title, cards in SECTIONS:
            label = QLabel(tr(title))
            label.setObjectName("sectionLabel")
            lay.addWidget(label)
            grid = QGridLayout()
            grid.setHorizontalSpacing(24)
            grid.setVerticalSpacing(22)
            for col in range(3):
                grid.setColumnStretch(col, 1)
            for i, (key, name, sub, primary) in enumerate(cards):
                card = ActionCard(key if key != "plans" else "plans", tr(name), tr(sub), self.theme, primary=primary)
                card.clicked.connect(partial(self.win.show_page, TARGET.get(key, key)))
                grid.addWidget(card, i // 3, i % 3)
            lay.addLayout(grid)
            lay.addSpacing(10)
        lay.addStretch(1)
