"""Scan filters, previews and recovery-to-another-volume."""
from __future__ import annotations

import hashlib
import os
import re
import string
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from .device import BlockSource
from .fs.base import FileEntry, Filesystem, FsError, UnsupportedError
from .license import License, LicenseRequired
from .safety import SafetyError, check_output_location

_INVALID = re.compile(r'[<>:"|?*\x00-\x1f]')
IMAGE_EXT = {"jpg", "jpeg", "png", "gif", "bmp", "webp"}


def filter_entries(entries: Iterable[FileEntry], *, exts: set[str] | None = None, min_size: int = 0,
                   max_size: int | None = None, since: datetime | None = None, until: datetime | None = None,
                   name_contains: str = "", deleted_only: bool = False) -> list[FileEntry]:
    out = []
    needle = name_contains.lower()
    for e in entries:
        if e.is_dir:
            continue
        if deleted_only and not e.deleted:
            continue
        if exts and e.extension not in exts:
            continue
        if e.size < min_size or (max_size is not None and e.size > max_size):
            continue
        if since and (e.mtime is None or e.mtime < since):
            continue
        if until and (e.mtime is None or e.mtime > until):
            continue
        if needle and needle not in e.path.lower():
            continue
        out.append(e)
    return out


def hex_dump(data: bytes, width: int = 16) -> str:
    lines = []
    for i in range(0, len(data), width):
        chunk = data[i:i + width]
        hexes = " ".join(f"{b:02x}" for b in chunk).ljust(width * 3 - 1)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{i:08x}  {hexes}  {text}")
    return "\n".join(lines)


def preview(fs: Filesystem, entry: FileEntry, limit: int = 64 * 1024) -> dict:
    """{'kind': 'image'|'text'|'hex', 'data': bytes|str} for a file, without writing anything."""
    data = fs.read(entry, 0, limit)
    if entry.extension in IMAGE_EXT or data[:4] in (b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1", b"\x89PNG", b"GIF8"):
        return {"kind": "image", "data": fs.read(entry, 0, min(entry.size, 16 * 1024 * 1024))}
    printable = set(string.printable.encode())
    if data and sum(b in printable for b in data) / len(data) > 0.95:
        return {"kind": "text", "data": data.decode("utf-8", "replace")}
    return {"kind": "hex", "data": hex_dump(data[:1024])}


def safe_relative_path(path: str) -> str:
    parts = []
    for p in path.replace("\\", "/").split("/"):
        p = _INVALID.sub("_", p).strip(" .")
        if p and p not in (".", ".."):
            parts.append(p[:120])
    return os.path.join(*parts) if parts else "_unnamed"


@dataclass
class RecoveredFile:
    entry: FileEntry
    dest: str = ""
    sha256: str = ""
    bytes_written: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass
class RecoveryReport:
    files: list[RecoveredFile] = field(default_factory=list)
    dry_run: bool = False

    @property
    def recovered(self) -> int:
        return sum(f.ok for f in self.files)

    @property
    def failed(self) -> int:
        return sum(not f.ok for f in self.files)


def recover(fs: Filesystem, entries: list[FileEntry], out_dir: str, *, source: BlockSource | None = None,
            license: License | None = None, dry_run: bool = False, keep_paths: bool = True,
            progress=None, disk_lookup=None) -> RecoveryReport:
    """Copy `entries` out of the filesystem into `out_dir`.

    Refuses when `out_dir` is on the source disk, and when the Free-tier size cap would be exceeded.
    """
    src = source or fs.source
    check_output_location(src, out_dir, disk_lookup=disk_lookup)
    if os.path.isfile(out_dir):
        raise SafetyError("Output location is a file, not a folder.")
    total = sum(e.size for e in entries if not e.is_dir)
    limit = license.recovery_limit() if license else None
    if limit is not None and total > limit:
        raise LicenseRequired("unlimited-recovery")
    report = RecoveryReport(dry_run=dry_run)
    used: set[str] = set()
    if not dry_run:
        os.makedirs(out_dir, exist_ok=True)
    for i, e in enumerate(entries):
        rel = safe_relative_path(e.path if keep_paths else e.name)
        dest = os.path.join(out_dir, "recovered" if e.deleted else "existing", rel)
        base, ext = os.path.splitext(dest)
        n = 1
        while dest.lower() in used or (not dry_run and os.path.exists(dest)):
            dest = f"{base} ({n}){ext}"
            n += 1
        used.add(dest.lower())
        rf = RecoveredFile(e, dest)
        report.files.append(rf)
        if dry_run:
            continue
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            h = hashlib.sha256()
            with open(dest, "wb") as out:
                done = 0
                while done < e.size:
                    block = fs.read(e, done, min(1 << 20, e.size - done))
                    if not block:
                        raise FsError("data ended early (clusters unreadable)")
                    out.write(block)
                    h.update(block)
                    done += len(block)
            rf.bytes_written, rf.sha256 = done, h.hexdigest()
            if e.mtime:
                ts = e.mtime.timestamp()
                os.utime(dest, (ts, ts))
        except (OSError, FsError, UnsupportedError) as ex:
            rf.error = str(ex)
        if progress:
            progress(i + 1, len(entries))
    return report
