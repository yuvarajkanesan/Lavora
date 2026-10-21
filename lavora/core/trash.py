"""Recycle Bin ($I/$R pairs), Linux trash (.trashinfo) and Volume Shadow Copy listing."""
from __future__ import annotations

import configparser
import os
import struct
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import unquote

from .fs.base import filetime_to_dt


@dataclass
class TrashItem:
    original_path: str
    size: int
    deleted_at: datetime | None
    stored_at: str  # where the data currently sits


def parse_dollar_i(raw: bytes) -> tuple[str, int, datetime | None]:
    """Parse a Windows $I metadata file (versions 1 and 2)."""
    version = struct.unpack_from("<Q", raw, 0)[0]
    size, ft = struct.unpack_from("<QQ", raw, 8)
    if version == 2:
        n = struct.unpack_from("<I", raw, 24)[0]
        path = raw[28:28 + 2 * n].decode("utf-16-le", "replace").rstrip("\0")
    elif version == 1:
        path = raw[24:].decode("utf-16-le", "replace").split("\0")[0]
    else:
        raise ValueError("unknown $I version")
    return path, size, filetime_to_dt(ft)


def list_recycle_bin(bin_root: str) -> list[TrashItem]:
    """`bin_root` is the volume's $Recycle.Bin folder (all SID sub-folders are searched)."""
    items = []
    for sid in os.listdir(bin_root):
        d = os.path.join(bin_root, sid)
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if name.startswith("$I"):
                try:
                    with open(os.path.join(d, name), "rb") as f:
                        path, size, when = parse_dollar_i(f.read())
                except (OSError, ValueError, struct.error):
                    continue
                items.append(TrashItem(path, size, when, os.path.join(d, "$R" + name[2:])))
    return items


def list_linux_trash(trash_dir: str) -> list[TrashItem]:
    items: list[TrashItem] = []
    info = os.path.join(trash_dir, "info")
    if not os.path.isdir(info):
        return items
    for name in os.listdir(info):
        if not name.endswith(".trashinfo"):
            continue
        cp = configparser.ConfigParser(interpolation=None)
        try:
            cp.read(os.path.join(info, name), encoding="utf-8")
            path = unquote(cp["Trash Info"]["Path"])
            when = datetime.fromisoformat(cp["Trash Info"].get("DeletionDate", ""))
        except (KeyError, ValueError, configparser.Error):
            path, when = "", None
        stored = os.path.join(trash_dir, "files", name[:-10])
        size = os.path.getsize(stored) if os.path.isfile(stored) else 0
        items.append(TrashItem(path, size, when, stored))
    return items


def list_shadow_copies() -> list[dict]:
    """Windows Volume Shadow Copies (needs an elevated prompt). Browse one via its DeviceObject path."""
    if sys.platform != "win32":
        return []
    ps = "Get-CimInstance Win32_ShadowCopy | Select-Object ID,DeviceObject,InstallDate,VolumeName | ConvertTo-Json"
    raw = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, check=False).stdout
    if not raw.strip():
        return []
    import json
    data = json.loads(raw)
    return data if isinstance(data, list) else [data]
