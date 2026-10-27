"""Automatic recovery: find every volume, open each filesystem with the first method that works, recover everything, and fall back
to signature carving. Every attempt is logged, so the report says what was tried, what failed and what worked.

Fallback chain (each step runs only if the previous one did not give a usable result):
  1. Find volumes:  partition table (MBR / GPT incl. its backup copy)  ->  filesystem at the start of the source  ->
                    scan the whole source for lost volumes (NTFS / FAT32 / exFAT / ext boot structures, NTFS backup boot sector)
  2. Open a volume: primary metadata  ->  backup boot sector (NTFS last/middle sector, FAT32 sector 6, exFAT sector 12, ext backup
                    superblock)  ->  NTFS $MFTMirr when $MFT record 0 is damaged  ->  combinations of the above
  3. Files:         everything the filesystem lists (existing + deleted); files lying in never-copied parts of a partial image
                    are kept apart
  4. Carving:       for volumes that could not be opened (or always in thorough mode): find files by content signature,
                    skipping anything already recovered through the filesystem (same SHA-256)
Nothing here writes to the source; the destination must be a different drive (checked first).
"""
from __future__ import annotations

import os
import struct
from collections.abc import Callable
from dataclasses import dataclass, field

from . import carver, report
from .device import SECTOR, BlockSource, PartitionView
from .fs import Filesystem, open_filesystem
from .fs.ntfs import NtfsFilesystem
from .license import License, LicenseRequired
from .partitions import identify_fs, read_partitions, scan_lost_partitions
from .recovery import recover
from .safety import check_output_location
from .views import OverlayView

CARVED_DIR = "Carved files (by content)"

PRESETS: dict[str, set[str]] = {
    "photos": {"jpg", "jpeg", "png", "gif", "bmp", "webp", "heic", "tif", "tiff", "raw", "cr2", "nef", "dng", "arw"},
    "documents": {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv", "odt", "ods", "odp", "rtf", "md", "zip"},
    "videos": {"mp4", "mov", "avi", "mkv", "wmv", "m4v", "mpg", "mpeg", "3gp"},
    "audio": {"mp3", "wav", "flac", "m4a", "ogg", "aac", "wma"},
}
_CARVE_FAMILY = {"jpeg": "jpg", "docx": "zip", "xlsx": "zip", "pptx": "zip", "mov": "mp4", "heic": "mp4", "m4a": "mp4", "3gp": "mp4"}


def parse_only(spec: str | None) -> set[str] | None:
    """'photos,pdf' -> {all photo extensions, 'pdf'}. None / 'all' / '' means no filter."""
    if not spec or spec.strip().lower() in ("all", "*"):
        return None
    out: set[str] = set()
    for tok in (t.strip().lower().lstrip(".") for t in spec.split(",")):
        out |= PRESETS.get(tok, {tok}) if tok else set()
    return out or None


@dataclass
class Step:
    stage: str
    status: str  # ok | failed | skipped | info
    detail: str = ""

    def to_dict(self) -> dict:
        return {"stage": self.stage, "result": self.status, "details": self.detail}


@dataclass
class VolumeResult:
    label: str
    start: int
    size: int
    fs: str = ""
    found_by: str = ""
    method: str = ""  # which way of opening the filesystem worked
    files: int = 0
    recovered: int = 0
    failed: int = 0
    incomplete: int = 0
    bytes: int = 0
    folder: str = ""
    error: str = ""
    done: int = 0  # files handled so far (live progress counter)

    def to_dict(self) -> dict:
        return {"volume": self.label, "found by": self.found_by, "opened with": self.method or "-", "files listed": self.files,
                "recovered": self.recovered, "failed": self.failed, "incomplete (partly unread)": self.incomplete,
                "bytes": self.bytes, "folder": self.folder, "error": self.error}


@dataclass
class AutoReport:
    out_dir: str
    steps: list[Step] = field(default_factory=list)
    volumes: list[VolumeResult] = field(default_factory=list)
    carved: int = 0
    carved_bytes: int = 0
    duplicates_skipped: int = 0
    cancelled: bool = False
    unread_bytes: int = 0
    report_path: str = ""

    @property
    def recovered(self) -> int:
        return sum(v.recovered for v in self.volumes) + self.carved

    @property
    def bytes(self) -> int:
        return sum(v.bytes for v in self.volumes) + self.carved_bytes

    @property
    def failed(self) -> int:
        return sum(v.failed for v in self.volumes)

    @property
    def incomplete(self) -> int:
        return sum(v.incomplete for v in self.volumes)


@dataclass
class _Cand:
    start: int
    size: int
    fs: str
    found_by: str


def _size_text(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return str(n)


# ---------------------------------------------------------------------------------------------------------------- opening a volume
def backup_replacements(view: BlockSource, size: int) -> list[tuple[str, dict[int, bytes]]]:
    """Backup copies of the boot structures that are still intact, each as (label, {offset: bytes}) to show at the primary position."""
    out: list[tuple[str, dict[int, bytes]]] = []

    def sector(off: int) -> bytes:
        return view.read_at(off, SECTOR) if 0 <= off <= size - SECTOR else b""

    last = sector(size - SECTOR)
    if last[3:11] == b"NTFS    " and last[510:512] == b"\x55\xaa":
        out.append(("NTFS backup boot sector (end of volume)", {0: last}))
    mid = sector(size // 2 // SECTOR * SECTOR)
    if mid[3:11] == b"NTFS    " and mid[510:512] == b"\x55\xaa":
        out.append(("NTFS backup boot sector (middle of volume)", {0: mid}))
    s6 = sector(6 * SECTOR)
    if s6[82:90] == b"FAT32   " and s6[510:512] == b"\x55\xaa":
        out.append(("FAT32 backup boot sector (sector 6)", {0: s6}))
    s12 = sector(12 * SECTOR)
    if s12[3:11] == b"EXFAT   " and s12[510:512] == b"\x55\xaa":
        out.append(("exFAT backup boot region (sector 12)", {0: s12}))
    for bs in (1024, 2048, 4096):  # ext backup superblock: first block of group 1
        group1 = {1024: 8193, 2048: 16384, 4096: 32768}[bs] * bs
        sb = view.read_at(group1, 1024) if group1 + 1024 <= size else b""
        if len(sb) == 1024 and struct.unpack_from("<H", sb, 56)[0] == 0xEF53 and (1024 << struct.unpack_from("<I", sb, 24)[0]) == bs:
            out.append((f"ext backup superblock ({bs} byte blocks)", {1024: sb}))
    return out


def _try_open(view: BlockSource, *, mirror: bool, cancel) -> tuple[Filesystem, bool]:
    """Open `view` and smoke-test the scan. Returns (filesystem, saw_a_file); raises on any failure."""
    fs: Filesystem = NtfsFilesystem(view, use_mirror=True) if mirror else open_filesystem(view)
    it = fs.scan(include_live=True, cancel=cancel)
    first = next(it, None)  # raises if the directory structures are unusable
    return fs, first is not None


def open_with_fallbacks(src: BlockSource, cand: _Cand, log: Callable[[str, str, str], None], cancel=None) -> tuple[Filesystem, BlockSource, str] | None:
    view = PartitionView(src, cand.start, cand.size)
    attempts: list[tuple[str, BlockSource, bool]] = [("primary metadata", view, False)]
    backups = backup_replacements(view, cand.size)
    for label, repl in backups:
        attempts.append((label, OverlayView(view, repl, label), False))
    maybe_ntfs = view.read_at(3, 8) == b"NTFS    " or any("NTFS" in b[0] for b in backups)
    if maybe_ntfs:
        attempts.append(("$MFT mirror ($MFTMirr)", view, True))
        for label, repl in backups:
            if "NTFS" in label:
                attempts.append((f"{label} + $MFT mirror", OverlayView(view, repl, label), True))
    empty_fallback: tuple[Filesystem, BlockSource, str] | None = None
    for label, v, mirror in attempts:
        if cancel and cancel():
            return None
        try:
            fs, saw_file = _try_open(v, mirror=mirror, cancel=cancel)
        except Exception as ex:  # noqa: BLE001 - any parser failure just means "try the next method"
            log(f"Open volume: {label}", "failed", f"{type(ex).__name__}: {ex}")
            continue
        if saw_file:
            log(f"Open volume: {label}", "ok", f"{fs.name} filesystem opened")
            return fs, v, label
        log(f"Open volume: {label}", "failed", "opened, but it lists no files; trying the next method")
        empty_fallback = empty_fallback or (fs, v, label)
    if empty_fallback:
        log("Open volume", "info", f"using '{empty_fallback[2]}' (a volume with no files)")
    return empty_fallback


# ---------------------------------------------------------------------------------------------------------------- finding volumes
def discover_volumes(src: BlockSource, log, cancel, progress, thorough: bool) -> list[_Cand]:
    cands: list[_Cand] = []
    try:
        scheme, parts = read_partitions(src)
    except Exception as ex:  # noqa: BLE001
        log("Find volumes: partition table", "failed", f"{type(ex).__name__}: {ex}")
        scheme, parts = "none", []
    real = [p for p in parts if p.size >= (1 << 20) and p.type_desc not in ("extended", "gpt-protective")]
    if real:
        cands = [_Cand(p.start, p.size, p.fs, f"{scheme} partition table") for p in real]
        log("Find volumes: partition table", "ok", f"{scheme} table with {len(real)} partition(s)")
    elif scheme != "none":
        log("Find volumes: partition table", "failed", f"{scheme} table found but it lists no usable partitions")
    else:
        log("Find volumes: partition table", "failed", "no valid MBR/GPT")
    if not cands:
        kind = identify_fs(src, 0)
        if kind:
            cands = [_Cand(0, src.size, kind, "filesystem at the start of the source (no partition table)")]
            log("Find volumes: filesystem at start", "ok", f"{kind} volume without a partition table")
    if not cands or thorough:
        log("Find volumes: scan for lost volumes", "info", "scanning the whole source for volume boot structures (slow on big disks; you can cancel)")
        try:
            found = scan_lost_partitions(src, progress=lambda d, t: progress("Looking for lost volumes", d, t), cancel=cancel)
        except Exception as ex:  # noqa: BLE001
            found = []
            log("Find volumes: scan for lost volumes", "failed", f"{type(ex).__name__}: {ex}")
        added = 0
        for f in found:
            if any(f.start < c.start + c.size and c.start < f.start + f.size for c in cands):  # overlaps a volume we already have
                continue
            cands.append(_Cand(f.start, f.size, f.fs, f"lost-volume scan ({f.confidence} confidence)"))
            added += 1
        log("Find volumes: scan for lost volumes", "ok" if added else "failed", f"{added} additional volume(s) found" if added else "no additional volumes found")
    return sorted(cands, key=lambda c: c.start)


# ---------------------------------------------------------------------------------------------------------------- the pipeline
def auto_recover(source: BlockSource, out_dir: str, *, include_live: bool = True, thorough: bool = False, license: License | None = None,
                 unread: list[tuple[int, int]] | None = None, progress: Callable[[str, int, int], None] | None = None,
                 cancel: Callable[[], bool] | None = None, on_step: Callable[[Step], None] | None = None,
                 only_exts: set[str] | None = None) -> AutoReport:
    """Recover everything recoverable from `source` into `out_dir` (a different drive)."""
    check_output_location(source, out_dir)
    os.makedirs(out_dir, exist_ok=True)
    rep = AutoReport(out_dir)
    if unread:
        rep.unread_bytes = sum(n for _o, n in unread)
    prog = progress or (lambda _s, _d, _t: None)

    def log(stage: str, status: str, detail: str = "") -> None:
        step = Step(stage, status, detail)
        rep.steps.append(step)
        if on_step:
            on_step(step)

    hashes: set[str] = set()
    unrecognised: list[tuple[str, _Cand]] = []
    try:
        cands = discover_volumes(source, log, cancel, prog, thorough)
        if cancel and cancel():
            rep.cancelled = True
            return _finish(rep)
        if not cands:
            cands = [_Cand(0, source.size, "", "whole source (no volumes found)")]
            log("Find volumes", "failed", "no volumes found: the whole source will be searched by file content")
        for idx, cand in enumerate(cands):
            label = f"Volume {idx + 1}"
            vr = VolumeResult(f"{label} ({_size_text(cand.size)})", cand.start, cand.size, cand.fs, cand.found_by)
            rep.volumes.append(vr)
            opened = open_with_fallbacks(source, cand, log, cancel)
            if cancel and cancel():
                rep.cancelled = True
                break
            if opened is None:
                vr.error = "no filesystem could be opened"
                log(f"{vr.label}", "failed", "no way of opening its filesystem worked; will search it by file content")
                unrecognised.append((vr.label, cand))
                continue
            fs, view, method = opened
            vr.method, vr.fs = method, fs.name
            vr.folder = os.path.join(out_dir, f"{label} ({fs.name}, {_size_text(cand.size)})")
            counter = {"seen": 0}

            def wanted(fs=fs, counter=counter):
                for e in fs.scan(include_live=include_live, cancel=cancel):
                    if e.is_dir:
                        continue
                    if only_exts and e.extension not in only_exts:
                        continue
                    counter["seen"] += 1
                    if e.recoverable:
                        yield e

            def on_file(rf, vr=vr, label=label):
                if rf.ok and rf.sha256:
                    hashes.add(rf.sha256)
                vr.done += 1
                prog(f"Recovering files from {label}", vr.done, 0)

            try:
                r = recover(fs, wanted(), vr.folder, source=source, license=license, cancel=cancel, keep_results=False, on_file=on_file,
                            unread=unread, volume_base=cand.start)
            except LicenseRequired as ex:
                vr.error = str(ex)
                log(f"Recover files: {vr.label}", "failed", str(ex))
                continue
            except Exception as ex:  # noqa: BLE001 - a broken volume must not stop the others
                vr.error = f"{type(ex).__name__}: {ex}"
                log(f"Recover files: {vr.label}", "failed", vr.error)
                unrecognised.append((vr.label, cand))
                continue
            vr.files, vr.recovered, vr.failed, vr.incomplete, vr.bytes = counter["seen"], r.recovered, r.failed, r.incomplete, r.bytes_written
            if r.stopped_reason:
                vr.error = r.stopped_reason
            log(f"Recover files: {vr.label}", "ok" if r.recovered or not counter["seen"] else "failed",
                f"{r.recovered} recovered ({_size_text(r.bytes_written)}), {r.failed} failed" + (f", {r.incomplete} incomplete (partly unread)" if r.incomplete else "")
                + (f"; {r.stopped_reason}" if r.stopped_reason else ""))
            if r.cancelled:
                rep.cancelled = True
                break
            if not r.recovered and counter["seen"] == 0:
                log(f"Recover files: {vr.label}", "info", "the filesystem lists no recoverable files; will search it by file content")
                unrecognised.append((vr.label, cand))

        # ---- carving: unrecognised volumes, or everything in thorough mode, or when nothing at all was recovered
        if not rep.cancelled:
            targets: list[tuple[str, BlockSource]] = []
            if thorough:
                targets = [("whole source", source)]
            elif unrecognised:
                targets = [(lbl, PartitionView(source, c.start, c.size)) for lbl, c in unrecognised]
            elif rep.recovered == 0:
                targets = [("whole source", source)]
            for lbl, view in targets:
                if cancel and cancel():
                    rep.cancelled = True
                    break
                _carve(rep, view, lbl, out_dir, hashes, license, log, prog, cancel, only_exts)
    finally:
        _finish(rep)
    return rep


def _carve(rep: AutoReport, view: BlockSource, label: str, out_dir: str, hashes: set[str], license, log, prog, cancel,
           only_exts: set[str] | None = None) -> None:
    limit = license.recovery_limit() if license else None
    found = 0
    log(f"Carve by file content: {label}", "info", "searching for files by signature (JPG, PNG, PDF, Office, ZIP, MP4, ...)")
    try:
        sigs = None
        if only_exts:
            wanted = {_CARVE_FAMILY.get(e, e) for e in only_exts}
            sigs = [sg for sg in carver.SIGNATURES if sg.ext in wanted]
        for cf in carver.carve(view, signatures=sigs, cancel=cancel, progress=lambda d, t: prog(f"Searching {label} by file content", d, t)):
            if limit is not None and rep.carved_bytes + cf.size > limit:
                log(f"Carve by file content: {label}", "failed", "Free-tier size limit reached; a Pro licence removes it")
                break
            path = carver.extract(view, cf, os.path.join(out_dir, CARVED_DIR, cf.sig.ext))
            if only_exts and os.path.splitext(path)[1].lstrip(".") not in only_exts:  # e.g. a ZIP that is a .docx when only .xlsx was asked for
                os.remove(path)
                continue
            if cf.sha256 in hashes:  # already recovered through the filesystem: do not keep a duplicate
                os.remove(path)
                rep.duplicates_skipped += 1
                continue
            hashes.add(cf.sha256)
            rep.carved += 1
            rep.carved_bytes += cf.size
            found += 1
    except Exception as ex:  # noqa: BLE001
        log(f"Carve by file content: {label}", "failed", f"{type(ex).__name__}: {ex}")
        return
    if cancel and cancel():
        rep.cancelled = True
    log(f"Carve by file content: {label}", "ok" if found else "info", f"{found} file(s) found by content" + (f" ({rep.duplicates_skipped} duplicates skipped)" if rep.duplicates_skipped else ""))


def _finish(rep: AutoReport) -> AutoReport:
    """Write the HTML report next to the recovered files."""
    try:
        data = report.build_report("Lavora automatic recovery", {
            "Summary": {"files recovered": rep.recovered, "total size": _size_text(rep.bytes), "failed": rep.failed,
                        "incomplete (partly unread)": rep.incomplete, "found by file content": rep.carved,
                        "never copied (partial image)": _size_text(rep.unread_bytes) if rep.unread_bytes else "-",
                        "stopped early": "yes" if rep.cancelled else "no"},
            "What was tried (in order)": [s.to_dict() for s in rep.steps],
            "Volumes": [v.to_dict() for v in rep.volumes] or [{"volume": "none found"}]})
        rep.report_path = os.path.join(rep.out_dir, "Lavora recovery report.html")
        report.save(data, rep.report_path)
    except OSError:
        rep.report_path = ""
    return rep


def summary_text(rep: AutoReport) -> str:
    lines = [f"{rep.recovered} file(s) recovered ({_size_text(rep.bytes)}) to {rep.out_dir}"]
    if rep.failed:
        lines.append(f"{rep.failed} file(s) could not be read")
    if rep.incomplete:
        lines.append(f"{rep.incomplete} file(s) are incomplete (partly in never-copied areas); see 'incomplete (partly unread)'")
    if rep.carved:
        lines.append(f"{rep.carved} file(s) found by content (no names or folders): {CARVED_DIR}")
    if rep.cancelled:
        lines.append("stopped before finishing")
    return "\n".join(lines)
