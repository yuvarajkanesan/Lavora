"""Minimal i18n: every user-visible string goes through tr(). English first; Tamil/Hindi catalogues come later."""
from __future__ import annotations

_CATALOGUES: dict[str, dict[str, str]] = {"en": {}}
_current = "en"


def set_language(code: str) -> None:
    global _current
    _current = code if code in _CATALOGUES else "en"


def register(code: str, strings: dict[str, str]) -> None:
    _CATALOGUES.setdefault(code, {}).update(strings)


def tr(text: str) -> str:
    return _CATALOGUES.get(_current, {}).get(text, text)
