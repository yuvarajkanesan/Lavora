"""Light/dark palettes and the application style sheet."""
from __future__ import annotations

PALETTES = {
    "light": {"bg": "#f6f5fb", "panel": "#ffffff", "text": "#1c1b22", "muted": "#6a6778", "accent": "#5b3fd1",
              "accent_text": "#ffffff", "border": "#dcd9e8", "danger": "#c62828", "ok": "#2e7d32"},
    "dark": {"bg": "#17161c", "panel": "#201f27", "text": "#ecebf3", "muted": "#9a97aa", "accent": "#8b73ff",
             "accent_text": "#14121f", "border": "#34323f", "danger": "#ef5350", "ok": "#66bb6a"},
}


def stylesheet(name: str) -> str:
    c = PALETTES[name]
    return f"""
QWidget {{ background: {c['bg']}; color: {c['text']}; font-size: 14px; }}
QFrame#card, QTableWidget, QPlainTextEdit, QLineEdit, QComboBox, QListWidget {{ background: {c['panel']}; border: 1px solid {c['border']}; border-radius: 8px; }}
QPushButton {{ background: {c['panel']}; border: 1px solid {c['border']}; border-radius: 8px; padding: 8px 14px; }}
QPushButton:hover {{ border-color: {c['accent']}; }}
QPushButton#primary {{ background: {c['accent']}; color: {c['accent_text']}; border: none; font-weight: 600; }}
QPushButton#tile {{ text-align: left; padding: 16px; min-height: 64px; font-size: 15px; }}
QLabel#muted {{ color: {c['muted']}; }}
QLabel#title {{ font-size: 24px; font-weight: 700; }}
QLabel#danger {{ color: {c['danger']}; }}
QHeaderView::section {{ background: {c['panel']}; border: none; border-bottom: 1px solid {c['border']}; padding: 6px; }}
QProgressBar {{ border: 1px solid {c['border']}; border-radius: 6px; text-align: center; }}
QProgressBar::chunk {{ background: {c['accent']}; border-radius: 5px; }}
"""
