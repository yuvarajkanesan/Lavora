"""Design tokens and the application style sheet (light and dark)."""
from __future__ import annotations

FONT_STACK = '"Inter", "Segoe UI Variable Text", "Segoe UI", "Helvetica Neue", Arial, sans-serif'

PALETTES = {
    "light": {
        "bg": "#f4f3fa", "panel": "#ffffff", "topbar": "#ffffff", "text": "#1b1836", "muted": "#5f5b78", "faint": "#8a86a0",
        "accent": "#5b3fd1", "accent_soft": "#ece9fb", "accent_text": "#ffffff", "accent_disabled": "#bcb1ee",
        "border": "#e3e0ef", "border_strong": "#cfcae4", "row_hover": "#f7f6fd", "row_selected": "#ece9fb",
        "ok": "#0f766e", "ok_soft": "#e2f3ee", "warn": "#a65f00", "warn_soft": "#fdf0dc", "danger": "#c62828", "danger_soft": "#fbe6e6",
        "track": "#e7e5f0", "choice_sel": "#f6f4ff",
    },
    "dark": {
        "bg": "#14131a", "panel": "#1e1d27", "topbar": "#1a1923", "text": "#ecebf5", "muted": "#a19eb8", "faint": "#7c7993",
        "accent": "#8b73ff", "accent_soft": "#2b2745", "accent_text": "#13111f", "accent_disabled": "#4a4270",
        "border": "#322f42", "border_strong": "#443f5c", "row_hover": "#24222f", "row_selected": "#2b2745",
        "ok": "#4fd1b5", "ok_soft": "#16302b", "warn": "#f0a840", "warn_soft": "#3a2c12", "danger": "#ff7a7a", "danger_soft": "#3b1c1c",
        "track": "#33304a", "choice_sel": "#241f3d",
    },
}


def stylesheet(name: str, check_png: str = "", arrow_png: str = "") -> str:
    c = PALETTES[name]
    tick = f"image: url({check_png});" if check_png else ""
    arrow = f"image: url({arrow_png}); width: 14px; height: 14px;" if arrow_png else ""
    return f"""
* {{ font-family: {FONT_STACK}; }}
QWidget {{ background: transparent; color: {c['text']}; font-size: 14px; }}
QMainWindow, QScrollArea, QScrollArea > QWidget > QWidget#pageBody {{ background: {c['bg']}; }}
QStackedWidget {{ background: transparent; }}
QWidget#root {{ background: {c['bg']}; }}
QScrollArea {{ border: none; }}

/* dialogs (message boxes, input dialogs): the global 'transparent' rule would otherwise leave them black */
QDialog, QMessageBox, QInputDialog {{ background: {c['panel']}; }}
QMessageBox QLabel, QInputDialog QLabel, QDialog QLabel {{ background: transparent; color: {c['text']}; font-size: 15px; }}
QMessageBox QPushButton, QInputDialog QPushButton, QDialog QPushButton {{ min-width: 90px; }}
QDialogButtonBox {{ background: transparent; }}

/* top bar */
QFrame#topbar {{ background: {c['topbar']}; border: none; border-bottom: 1px solid {c['border']}; }}
QLabel#brand {{ font-size: 21px; font-weight: 700; }}
QLabel#version {{ color: {c['muted']}; font-size: 15px; }}
QLabel#pill {{ background: {c['accent_soft']}; color: {c['accent']}; border-radius: 15px; padding: 7px 16px; font-weight: 600; font-size: 14px; }}
QLabel#pillOk {{ background: {c['ok_soft']}; color: {c['ok']}; border-radius: 15px; padding: 7px 16px; font-weight: 600; font-size: 14px; }}

/* headings */
QLabel#hero {{ font-size: 42px; font-weight: 800; }}
QLabel#title {{ font-size: 34px; font-weight: 800; }}
QLabel#lead {{ color: {c['muted']}; font-size: 17px; }}
QLabel#sectionLabel {{ color: {c['muted']}; font-size: 13px; font-weight: 700; letter-spacing: 2px; }}
QLabel#muted {{ color: {c['muted']}; }}
QLabel#faint {{ color: {c['faint']}; }}
QLabel#h3 {{ font-size: 17px; font-weight: 700; }}
QLabel#fieldLabel {{ font-size: 15px; font-weight: 700; }}
QLabel#crumbMuted {{ color: {c['muted']}; font-size: 16px; }}
QPushButton#crumb {{ background: transparent; border: none; color: {c['accent']}; font-size: 16px; padding: 0 4px; min-height: 28px; text-align: left; }}
QPushButton#crumb:hover {{ text-decoration: underline; }}

/* cards */
QFrame#card {{ background: {c['panel']}; border: 1px solid {c['border']}; border-radius: 18px; }}
QFrame#card QLabel {{ background: transparent; }}
QFrame#actionCard {{ background: {c['panel']}; border: 1px solid {c['border']}; border-radius: 18px; }}
QFrame#actionCard:hover {{ border-color: {c['accent']}; }}
QFrame#actionCard[primary="true"] {{ background: {c['accent']}; border: 1px solid {c['accent']}; }}
QFrame#actionCard QLabel {{ background: transparent; }}
QLabel#cardTitle {{ font-size: 19px; font-weight: 700; }}
QLabel#cardSub {{ color: {c['muted']}; font-size: 15px; }}
QFrame#actionCard[primary="true"] QLabel#cardTitle, QFrame#actionCard[primary="true"] QLabel#cardSub {{ color: {c['accent_text']}; }}
QFrame#choice {{ background: {c['panel']}; border: 1px solid {c['border']}; border-radius: 14px; }}
QFrame#choice[selected="true"] {{ background: {c['choice_sel']}; border: 2px solid {c['accent']}; }}
QFrame#choice QLabel {{ background: transparent; }}
QLabel#choiceTitle {{ font-size: 17px; font-weight: 700; }}
QLabel#choiceSub {{ color: {c['muted']}; font-size: 14px; }}
QFrame#chip {{ background: {c['panel']}; border: 1px solid {c['border']}; border-radius: 18px; }}
QFrame#chip QLabel {{ background: transparent; font-size: 15px; }}
QFrame#preview {{ background: {c['bg']}; border: 1px dashed {c['border_strong']}; border-radius: 12px; }}
QFrame#preview QLabel {{ background: transparent; }}

/* banners */
QFrame#banner {{ border: none; border-top-left-radius: 18px; border-top-right-radius: 18px; }}
QFrame#banner[kind="success"] {{ background: {c['ok_soft']}; }}
QFrame#banner[kind="info"] {{ background: {c['accent_soft']}; }}
QFrame#banner[kind="warn"] {{ background: {c['warn_soft']}; }}
QFrame#banner[kind="error"] {{ background: {c['danger_soft']}; }}
QFrame#banner QLabel {{ background: transparent; font-size: 16px; font-weight: 600; }}
QFrame#banner[kind="success"] QLabel {{ color: {c['ok']}; }}
QFrame#banner[kind="info"] QLabel {{ color: {c['accent']}; }}
QFrame#banner[kind="warn"] QLabel {{ color: {c['warn']}; }}
QFrame#banner[kind="error"] QLabel {{ color: {c['danger']}; }}

/* inputs */
QLineEdit, QComboBox, QPlainTextEdit, QTextEdit {{ background: {c['panel']}; border: 1px solid {c['border_strong']}; border-radius: 12px;
    padding: 10px 14px; selection-background-color: {c['accent_soft']}; selection-color: {c['text']}; }}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {{ border: 1px solid {c['accent']}; }}
QComboBox {{ padding-right: 28px; }}
QComboBox::drop-down {{ border: none; width: 34px; }}
QComboBox::down-arrow {{ {arrow} }}
QLineEdit, QComboBox {{ min-height: 24px; }}
QComboBox QAbstractItemView {{ background: {c['panel']}; border: 1px solid {c['border']}; selection-background-color: {c['accent_soft']}; selection-color: {c['text']}; }}
QCheckBox {{ spacing: 10px; }}
QCheckBox::indicator {{ width: 20px; height: 20px; border: 1.5px solid {c['border_strong']}; border-radius: 6px; background: {c['panel']}; }}
QCheckBox::indicator:checked {{ background: {c['accent']}; border-color: {c['accent']}; {tick} }}
QTableView::indicator {{ width: 20px; height: 20px; border: 1.5px solid {c['border_strong']}; border-radius: 6px; background: {c['panel']}; }}
QTableView::indicator:checked {{ background: {c['accent']}; border-color: {c['accent']}; {tick} }}

/* buttons */
QPushButton {{ background: {c['panel']}; border: 1px solid {c['border_strong']}; border-radius: 12px; padding: 0 18px; min-height: 46px; font-weight: 600; }}
QPushButton:hover {{ border-color: {c['accent']}; }}
QPushButton:disabled {{ color: {c['faint']}; }}
QPushButton#primary {{ background: {c['accent']}; color: {c['accent_text']}; border: none; font-size: 16px; padding: 0 22px; min-height: 52px; }}
QPushButton#primary:hover {{ background: {c['accent']}; }}
QPushButton#primary:disabled {{ background: {c['accent_disabled']}; color: {c['accent_text']}; }}
QPushButton#chipBtn {{ background: {c['panel']}; border: 1px solid {c['border_strong']}; border-radius: 22px; padding: 0 22px; min-height: 44px; max-height: 44px; font-weight: 500; font-size: 15px; }}
QPushButton#chipBtn:checked {{ background: {c['accent_soft']}; border: 1px solid {c['accent']}; color: {c['accent']}; font-weight: 700; }}
QPushButton#link {{ background: transparent; border: none; color: {c['accent']}; padding: 0 6px; min-height: 28px; font-weight: 600; }}
QPushButton#danger {{ color: {c['danger']}; }}

/* tables */
QTableWidget {{ background: {c['panel']}; border: none; border-radius: 0; gridline-color: transparent; outline: 0; alternate-background-color: {c['panel']}; }}
QTableWidget::item {{ border-bottom: 1px solid {c['border']}; padding: 6px 8px; }}
QTableWidget::item:hover {{ background: {c['row_hover']}; }}
QTableWidget::item:selected {{ background: {c['row_selected']}; color: {c['text']}; }}
QHeaderView {{ background: {c['panel']}; }}
QHeaderView::section {{ background: {c['bg']}; color: {c['muted']}; border: none; border-bottom: 1px solid {c['border']}; padding: 14px 10px;
    font-size: 13px; font-weight: 700; text-align: left; }}
QScrollBar:vertical {{ background: transparent; width: 12px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {c['border_strong']}; border-radius: 5px; min-height: 30px; }}
QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {c['border_strong']}; border-radius: 5px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QProgressBar {{ background: {c['track']}; border: none; border-radius: 5px; max-height: 10px; min-height: 10px; text-align: center; color: transparent; }}
QProgressBar::chunk {{ background: {c['accent']}; border-radius: 5px; }}
QToolTip {{ background: {c['panel']}; color: {c['text']}; border: 1px solid {c['border']}; padding: 6px; }}
"""
