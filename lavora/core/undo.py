"""Snapshot-before-write: the only way Lavora modifies a disk or image.

write_regions() saves the bytes that are about to be replaced, writes, and records an undo id. undo() puts the
original bytes back. Every repair feature goes through here, so "Apply" always has an "Undo".
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass

from .device import SECTOR
from .paths import undo_dir
from .safety import SafetyError


@dataclass
class WriteResult:
    undo_id: str | None
    applied: bool
    plan: list[tuple[int, int]]  # (offset, length) of every region touched


def _check_aligned(regions: list[tuple[int, bytes]]) -> None:
    for off, data in regions:
        if off < 0 or off % SECTOR or len(data) % SECTOR or not data:
            raise SafetyError(f"Write at {off} (+{len(data)}) is not sector aligned.")


def write_regions(target: str, regions: list[tuple[int, bytes]], description: str, *, dry_run: bool = True,
                  store: str | None = None) -> WriteResult:
    """Replace `regions` ((offset, new bytes) pairs) on `target`, after saving the old bytes. Dry-run by default."""
    _check_aligned(regions)
    plan = [(off, len(data)) for off, data in regions]
    if dry_run:
        return WriteResult(None, False, plan)
    store = store or undo_dir()
    undo_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    folder = os.path.join(store, undo_id)
    os.makedirs(folder)
    with open(target, "rb") as fh:
        saved = []
        blob = bytearray()
        for off, data in regions:
            fh.seek(off)
            old = fh.read(len(data))
            saved.append({"offset": off, "length": len(old), "blob_offset": len(blob)})
            blob += old
    with open(os.path.join(folder, "data.bin"), "wb") as f:
        f.write(blob)
    with open(os.path.join(folder, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"target": os.path.abspath(target) if not target.startswith("\\\\.\\") else target,
                   "description": description, "created": time.time(), "regions": saved}, f, indent=2)
    with open(target, "r+b", buffering=0 if target.startswith(("\\\\.\\", "/dev/")) else -1) as fh:
        for off, data in regions:
            fh.seek(off)
            fh.write(data)
        fh.flush()
        if not target.startswith(("\\\\.\\", "/dev/")):
            os.fsync(fh.fileno())
    return WriteResult(undo_id, True, plan)


def list_snapshots(store: str | None = None) -> list[dict]:
    store = store or undo_dir()
    out = []
    for name in sorted(os.listdir(store)):
        mf = os.path.join(store, name, "manifest.json")
        if os.path.isfile(mf):
            with open(mf, encoding="utf-8") as f:
                m = json.load(f)
            m["id"] = name
            out.append(m)
    return out


def undo(undo_id: str, *, store: str | None = None) -> str:
    """Restore the bytes saved under `undo_id`. Returns the target that was restored."""
    store = store or undo_dir()
    folder = os.path.join(store, undo_id)
    with open(os.path.join(folder, "manifest.json"), encoding="utf-8") as f:
        m = json.load(f)
    with open(os.path.join(folder, "data.bin"), "rb") as f:
        blob = f.read()
    with open(m["target"], "r+b", buffering=0 if m["target"].startswith(("\\\\.\\", "/dev/")) else -1) as fh:
        for r in m["regions"]:
            fh.seek(r["offset"])
            fh.write(blob[r["blob_offset"]:r["blob_offset"] + r["length"]])
        fh.flush()
    return m["target"]
