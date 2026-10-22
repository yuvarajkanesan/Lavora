"""Scan filters, previews and recovery-to-another-volume."""
from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import re
import string
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime

from .device import BlockSource
from .fs.base import FileEntry, Filesystem, FsError, UnsupportedError
from .license import License, LicenseRequired
from .safety import SafetyError, check_output_location

_INVALID = re.compile(r'[<>:"|?*\x00-\x1f]')
IMAGE_EXT = {"jpg", "jpeg", "png", "gif", "bmp", "webp"}


def iter_entries(entries: Iterable[FileEntry], *, exts: set[str] | None = None, min_size: int = 0,
                 max_size: int | None = None, since: datetime | None = None, until: datetime | None = None,
                 name_contains: str = "", deleted_only: bool = False) -> Iterator[FileEntry]:
    """Lazy version of filter_entries: works on a scan that is still streaming (millions of entries, constant memory)."""
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
        yield e


def filter_entries(entries: Iterable[FileEntry], **kw) -> list[FileEntry]:
    return list(iter_entries(entries, **kw))


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
    files: list[RecoveredFile] = field(default_factory=list)  # all results, or just failures when keep_results=False
    dry_run: bool = False
    recovered: int = 0
    failed: int = 0
    bytes_written: int = 0
    cancelled: bool = False
    stopped_reason: str = ""  # e.g. the destination is full


class Estimate:
    """Chance-of-recovery summary, accumulated entry by entry so it works on a streaming scan."""

    def __init__(self) -> None:
        self.files = self.deleted = self.good = self.partial = self.lost = self.unsupported = 0
        self.bytes_good = self.bytes_partial = 0

    def add(self, e: FileEntry) -> None:
        if e.is_dir:
            return
        self.files += 1
        if not e.deleted:
            return
        self.deleted += 1
        if e.note.startswith("unsupported"):
            self.unsupported += 1
        elif e.overwritten_pct >= 100:
            self.lost += 1
        elif e.overwritten_pct > 0:
            self.partial += 1
            self.bytes_partial += e.size
        else:
            self.good += 1
            self.bytes_good += e.size

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def estimate(entries: Iterable[FileEntry]) -> dict:
    est = Estimate()
    for e in entries:
        est.add(e)
    return est.as_dict()


def recover(fs: Filesystem, entries: Iterable[FileEntry], out_dir: str, *, source: BlockSource | None = None,
            license: License | None = None, dry_run: bool = False, keep_paths: bool = True, progress=None,
            byte_progress=None, cancel: Callable[[], bool] | None = None, on_file=None, keep_results: bool = True,
            disk_lookup=None) -> RecoveryReport:
    """Copy `entries` out of the filesystem into `out_dir`, streaming (any number of files, any file size).

    Refuses when `out_dir` is on the source disk, and when the Free-tier size cap would be exceeded. `cancel()` is polled
    every MiB, so even a multi-terabyte file stops within a moment; `byte_progress(done, size)` reports inside a file.
    A full destination stops the run instead of failing every remaining file.
    """
    src = source or fs.source
    check_output_location(src, out_dir, disk_lookup=disk_lookup)
    if os.path.isfile(out_dir):
        raise SafetyError("Output location is a file, not a folder.")
    limit = license.recovery_limit() if license else None
    if limit is not None:  # the cap needs the total up front
        entries = list(entries)
        if sum(e.size for e in entries if not e.is_dir) > limit:
            raise LicenseRequired("unlimited-recovery")
    report = RecoveryReport(dry_run=dry_run)
    total_files = len(entries) if isinstance(entries, list) else 0
    used: set[str] = set()
    if not dry_run:
        os.makedirs(out_dir, exist_ok=True)
    for i, e in enumerate(entries):
        if cancel and cancel():
            report.cancelled = True
            break
        rel = safe_relative_path(e.path if keep_paths else e.name)
        dest = os.path.join(out_dir, "recovered" if e.deleted else "existing", rel)
        base, ext = os.path.splitext(dest)
        n = 1
        while dest.lower() in used or (not dry_run and os.path.exists(dest)):
            dest = f"{base} ({n}){ext}"
            n += 1
        if len(used) < 2_000_000:  # name de-duplication memory is bounded; the exists() check covers the rest
            used.add(dest.lower())
        rf = RecoveredFile(e, dest)
        if not dry_run:
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                h = hashlib.sha256()
                with open(dest, "wb") as out:
                    done = 0
                    while done < e.size:
                        if cancel and cancel():
                            raise _Cancelled
                        block = fs.read(e, done, min(1 << 20, e.size - done))
                        if not block:
                            raise FsError("data ended early (clusters unreadable)")
                        out.write(block)
                        h.update(block)
                        done += len(block)
                        if byte_progress:
                            byte_progress(done, e.size)
                rf.bytes_written, rf.sha256 = done, h.hexdigest()
                report.bytes_written += done
                if e.mtime:
                    ts = e.mtime.timestamp()
                    os.utime(dest, (ts, ts))
            except _Cancelled:
                report.cancelled = True
                with contextlib.suppress(OSError):
                    os.remove(dest)  # never leave a truncated file that looks like a recovery
                break
            except (OSError, FsError, UnsupportedError) as ex:
                rf.error = str(ex)
                if isinstance(ex, OSError) and ex.errno == errno.ENOSPC:
                    report.stopped_reason = "The destination drive is full."
                    with contextlib.suppress(OSError):
                        os.remove(dest)
                    report.failed += 1
                    report.files.append(rf)
                    break
        if rf.ok:
            report.recovered += 1
        else:
            report.failed += 1
        if keep_results or not rf.ok:
            report.files.append(rf)
        if on_file:
            on_file(rf)
        if progress:
            progress(i + 1, total_files)
    return report


class _Cancelled(Exception):
    pass
