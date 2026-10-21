"""Where Lavora keeps its own data (undo snapshots, audit log, settings). LAVORA_HOME overrides it (used by tests)."""
from __future__ import annotations

import os
import sys

from ..brand import DATA_DIR_NAME


def data_dir() -> str:
    override = os.environ.get("LAVORA_HOME")
    if override:
        base = override
    elif sys.platform == "win32":
        base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), DATA_DIR_NAME)
    else:
        base = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), DATA_DIR_NAME.lower())
    os.makedirs(base, exist_ok=True)
    return base


def sub_dir(name: str) -> str:
    path = os.path.join(data_dir(), name)
    os.makedirs(path, exist_ok=True)
    return path


def undo_dir() -> str:
    return sub_dir("undo")


def audit_log_path() -> str:
    return os.path.join(data_dir(), "audit.log")
