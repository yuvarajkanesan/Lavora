"""Sector-by-sector imaging with a ddrescue-style bad-sector map, pause/resume, compression, hashing and cloning.

Pass 1 copies forward in large blocks; when reads keep failing it jumps ahead (a dead zone is skipped, not ground through).
Pass 2 re-reads what was skipped or failed, block by block, probing a failed block before dissecting it down to sectors.
Pass 3 retries the remaining bad ranges in reverse order. Progress is kept in a
JSON map file next to the image, so an interrupted job resumes where it stopped and the map documents what is lost.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .device import SECTOR, BlockSource
from .safety import SafetyError, require_typed_confirmation

Range = tuple[int, int]  # (offset, length)


def merge_ranges(ranges: list[Range]) -> list[Range]:
    out: list[Range] = []
    for off, ln in sorted(ranges):
        if out and off <= out[-1][0] + out[-1][1]:
            end = max(out[-1][0] + out[-1][1], off + ln)
            out[-1] = (out[-1][0], end - out[-1][0])
        else:
            out.append((off, ln))
    return out


@dataclass
class ImageResult:
    path: str
    size: int
    bad: list[Range] = field(default_factory=list)
    complete: bool = False
    cancelled: bool = False
    sha256: str = ""
    passes_done: int = 0

    @property
    def bad_bytes(self) -> int:
        return sum(ln for _o, ln in self.bad)


class ImageJob:
    def __init__(self, source: BlockSource, dest: str, *, block: int | None = None, passes: int = 3,
                 mapfile: str | None = None, compress: bool = False, hash_result: bool = False, cancel_fn=None):
        # physical disks get smaller reads: on a failing drive one unreadable block can cost many seconds of retries inside the
        # driver, so a smaller block bounds the damage and keeps progress visible
        block = block or ((256 << 10) if source.is_device else (1 << 20))
        if block % SECTOR:
            raise ValueError("block size must be a multiple of 512")
        if os.path.exists(dest) and not mapfile and os.path.getsize(dest) and not os.path.exists(dest + ".map"):
            raise SafetyError(f"{dest} already exists; refusing to overwrite an existing image.")
        self.source, self.dest = source, dest
        self.block, self.passes, self.compress, self.hash_result = block, passes, compress, hash_result
        self.cancel_fn = cancel_fn
        self.pos = 0  # offset being read right now (for live status)
        self.errors: dict[str, int] = {}  # what the drive/OS actually said, with counts (kept in the map file too)
        self.last_error = ""
        self.last_read_s = 0.0  # how long the last block took: a failing disk shows up here
        self.mapfile = mapfile or dest + ".map"
        self._run = threading.Event()
        self._run.set()
        self._cancel = False
        self.state: dict = {}

    # ---------------------------------------------------------------- controls
    def pause(self) -> None:
        self._run.clear()

    def resume(self) -> None:
        self._run.set()

    def cancel(self) -> None:
        self._cancel = True
        self._run.set()

    # ---------------------------------------------------------------- map file
    def _load(self) -> None:
        if os.path.exists(self.mapfile):
            with open(self.mapfile, encoding="utf-8") as f:
                st = json.load(f)
            if st.get("size") == self.source.size:
                self.state = st
                return
        self.state = {"size": self.source.size, "pass": 1, "next": 0, "suspect": [], "bad": []}

    def _save(self) -> None:
        tmp = self.mapfile + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f)
        os.replace(tmp, self.mapfile)

    def _checkpoint(self) -> bool:
        """Honour pause/cancel; True means stop."""
        self._run.wait()
        return self._cancel or bool(self.cancel_fn and self.cancel_fn())

    # ---------------------------------------------------------------- the copy
    def _check_space(self) -> None:
        """Fail at once, with a clear message, instead of discovering a full destination hours in."""
        if self.compress:
            return
        have = os.path.getsize(self.dest) if os.path.exists(self.dest) else 0
        need = max(0, self.source.size - have)
        folder = os.path.dirname(os.path.abspath(self.dest)) or "."
        free = shutil.disk_usage(folder).free
        if need + (64 << 20) > free:
            gib = 1 << 30
            raise OSError(f"Not enough free space on the destination: the image needs {need / gib:.0f} GiB but only {free / gib:.0f} GiB is free. "
                          "Choose a bigger drive, or turn on compression.")

    def _ensure_size(self, out) -> None:
        """Make the image exactly as long as the source. POSIX truncate() is an instant sparse extend; on Windows it zero-fills the
        file as it grows (hours for a 1 TB image), so there the file just grows as blocks are written and only a missing tail is
        padded here."""
        size = self.source.size
        if sys.platform != "win32":
            out.truncate(size)
            return
        out.seek(0, os.SEEK_END)
        pos = out.tell()
        zeros = bytes(8 << 20)
        while pos < size:
            if self._checkpoint():
                return
            n = min(len(zeros), size - pos)
            out.write(zeros[:n])
            pos += n

    def run(self, progress: Callable[[int, int], None] | None = None) -> ImageResult:
        self._load()
        size = self.source.size
        self._check_space()
        mode = "r+b" if os.path.exists(self.dest) else "w+b"
        with open(self.dest, mode) as out:
            if sys.platform != "win32":
                out.truncate(size)
            if self.state["pass"] == 1 and not self._pass1(out, progress):
                return self._result(False, True)
            if self.state["pass"] == 2 and self.passes >= 2 and not self._refine(out, 2, self.state["suspect"], self.block, progress):
                return self._result(False, True)
            for p in range(3, self.passes + 1):
                if self.state["pass"] == p and not self._refine(out, p, self.state["bad"], 64 << 10, progress, reverse=True):
                    return self._result(False, True)
            self._ensure_size(out)
        self.state["pass"] = self.passes + 1
        self._save()
        res = self._result(True, False)
        if self.compress:
            res.path = self._compress()
        if self.hash_result and not res.bad:
            res.sha256 = sha256_of(self.source)
        return res

    def _result(self, complete: bool, cancelled: bool) -> ImageResult:
        bad = [tuple(r) for r in self.state["bad"]] + [tuple(r) for r in self.state["suspect"]]
        return ImageResult(self.dest, self.source.size, merge_ranges(bad), complete and not bad, cancelled,
                           passes_done=min(self.state["pass"] - 1, self.passes))

    # ---------------------------------------------------------------- low-level helpers
    def _note_error(self, ex: OSError) -> None:
        msg = f"{type(ex).__name__}" + (f" [Errno {ex.errno}]" if ex.errno else "") + f": {ex.strerror or ex}"
        self.last_error = msg
        if msg in self.errors or len(self.errors) < 20:
            self.errors[msg] = self.errors.get(msg, 0) + 1
        self.state["errors"] = dict(self.errors)

    def _try(self, out, off: int, n: int) -> bool:
        """Read [off, off+n) and write it to the image. False (and the error is recorded) if the source refuses."""
        try:
            data = self.source.read_exact(off, n)
        except OSError as ex:
            self._note_error(ex)
            return False
        out.seek(off)
        out.write(data)
        return True

    @staticmethod
    def _add(ranges: list[Range], off: int, ln: int) -> None:
        """Append a range, merging with the previous one when contiguous (a 40 GB dead zone stays ONE entry)."""
        if ranges and ranges[-1][0] + ranges[-1][1] == off:
            ranges[-1] = (ranges[-1][0], ranges[-1][1] + ln)
        else:
            ranges.append((off, ln))

    def _probe_dead(self, out, off: int, n: int) -> bool:
        """True if the first, middle and last few KiB of the span all fail: the whole span is almost certainly unreadable,
        so there is no point dissecting it sector by sector. Any probe that succeeds is kept."""
        step = min(4096, n)
        spots = {off, off + max(0, (n - step) // 2 // SECTOR * SECTOR), off + max(0, (n - step) // SECTOR * SECTOR)}
        dead = True
        for o in sorted(spots):
            if self._try(out, o, min(step, off + n - o)):
                dead = False
        return dead

    # ---------------------------------------------------------------- pass 1
    def _pass1(self, out, progress) -> bool:
        pos = self.state["next"]
        size = self.source.size
        suspect = [tuple(r) for r in self.state["suspect"]]
        n = streak = 0
        skip = 0
        while pos < size:
            if self._checkpoint():
                self.state["suspect"] = [list(r) for r in suspect]
                self.state["next"] = pos
                self._save()
                return False
            ln = min(self.block, size - pos)
            self.pos = pos
            t0 = time.perf_counter()
            ok = self._try(out, pos, ln)
            self.last_read_s = time.perf_counter() - t0
            pos += ln
            if ok:
                streak = skip = 0
            else:
                self._add(suspect, pos - ln, ln)
                streak += 1
                if streak >= 8 and pos < size:  # a dead zone: jump ahead, growing the jump; pass 2 revisits what was skipped
                    skip = min(max(skip * 2, 4 * self.block), 1 << 30)
                    jump = min(skip, size - pos) // SECTOR * SECTOR
                    if jump:
                        self._add(suspect, pos, jump)
                        pos += jump
            n += 1
            if progress:
                progress(pos, size)
            if n % 64 == 0:
                self.state.update(next=pos, suspect=[list(r) for r in suspect])
                self._save()
        self.state.update(next=size, suspect=[list(r) for r in merge_ranges(suspect)])
        self.state["pass"] = 2
        self._save()
        return True

    # ---------------------------------------------------------------- passes 2 and 3
    def _refine(self, out, pass_no: int, todo: list, block: int, progress, reverse: bool = False) -> bool:
        """Re-read `todo` ranges block by block. A failed block is probed; only if some probe works is it dissected into 4 KiB
        steps and then sectors. After 16 dead blocks in a row the pass samples ahead (one probe per 16 blocks) so a huge dead
        zone costs minutes, not days. Returns False if cancelled (state saved so the next run resumes)."""
        ordered = sorted((tuple(r) for r in todo), reverse=reverse)
        bad: list[Range] = [tuple(r) for r in self.state["bad"]] if pass_no == 2 else []  # pass 3 rebuilds the list from scratch
        for idx, (off, ln) in enumerate(ordered):
            end = off + ln
            pos, streak = (off, 0) if not reverse else (end, 0)
            while (pos < end) if not reverse else (pos > off):
                if self._checkpoint():
                    rest = [(pos, end - pos)] if not reverse and pos < end else ([(off, pos - off)] if reverse and pos > off else [])
                    rest += ordered[idx + 1:]
                    self._save_progress(pass_no, rest, bad)
                    return False
                n = min(block, end - pos) if not reverse else min(block, pos - off)
                start = pos if not reverse else pos - n
                self.pos = start
                if self._try(out, start, n):
                    streak = 0
                elif not self._probe_dead(out, start, n):
                    for c in range(start, start + n, 4096):  # something here is readable: go finer
                        cn = min(4096, start + n - c)
                        if not self._try(out, c, cn):
                            for sct in range(c, c + cn, SECTOR):
                                sn = min(SECTOR, c + cn - sct)
                                if not self._try(out, sct, sn):
                                    self._add(bad, sct, sn)
                    streak = 0
                else:
                    self._add(bad, start, n)
                    streak += 1
                pos = pos + n if not reverse else pos - n
                if streak >= 16:  # dead zone: sample ahead
                    window = 16 * block
                    while (pos < end) if not reverse else (pos > off):
                        w = min(window, end - pos) if not reverse else min(window, pos - off)
                        ws = pos if not reverse else pos - w
                        pn = min(block, w)
                        if self._try(out, ws if not reverse else ws + w - pn, pn):
                            break  # readable again: back to block-by-block (the window edge is retried by pass 3)
                        self._add(bad, ws, w)
                        pos = pos + w if not reverse else pos - w
                    streak = 0
                if progress:
                    progress(min(max(pos, off), end), self.source.size)
        self.state["suspect"] = []
        self.state["bad"] = [list(r) for r in merge_ranges(bad)]
        self.state["pass"] = pass_no + 1
        self._save()
        return True

    def _save_progress(self, pass_no: int, rest: list[Range], bad: list[Range]) -> None:
        if pass_no == 2:
            self.state["suspect"] = [list(r) for r in merge_ranges(rest)]
            self.state["bad"] = [list(r) for r in merge_ranges(bad)]
        else:  # pass 3: whatever is not retried yet stays bad, plus what failed again
            self.state["bad"] = [list(r) for r in merge_ranges(rest + bad)]
        self._save()

    def _compress(self) -> str:
        gz = self.dest + ".gz"
        with open(self.dest, "rb") as f, gzip.open(gz, "wb", compresslevel=6) as g:
            shutil.copyfileobj(f, g, 1 << 20)
        os.remove(self.dest)
        return gz


# ---------------------------------------------------------------- hashing / cloning
def sha256_of(source: BlockSource, skip: list[Range] | None = None, chunk: int = 1 << 20, cancel=None, progress=None) -> str:
    """SHA-256 of the whole source (a full read: optional for huge disks). Returns '' if cancelled."""
    h = hashlib.sha256()
    pos = 0
    skip = merge_ranges(skip or [])
    while pos < source.size:
        if cancel and cancel():
            return ""
        if progress:
            progress(pos, source.size)
        n = min(chunk, source.size - pos)
        for so, sl in skip:  # bad ranges are hashed as zeros on both sides
            if so < pos + n and so + sl > pos:
                data = bytearray(source.read_at(pos, n).ljust(n, b"\0"))
                for so2, sl2 in skip:
                    a, b = max(so2, pos), min(so2 + sl2, pos + n)
                    if a < b:
                        data[a - pos:b - pos] = bytes(b - a)
                h.update(bytes(data))
                break
        else:
            h.update(source.read_at(pos, n))
        pos += n
    return h.hexdigest()


def verify_copy(source: BlockSource, copy: BlockSource, skip: list[Range] | None = None) -> bool:
    return source.size == copy.size and sha256_of(source, skip) == sha256_of(copy, skip)


def clone(source: BlockSource, target: str, *, confirmation: str | None = None, block: int = 1 << 20,
          progress=None, cancel=None) -> ImageResult:
    """Copy `source` onto `target` (a device or file). Writing to a physical disk needs the typed phrase
    'ERASE <target>'; the target must never be the source and must be at least as large."""
    if os.path.normcase(os.path.abspath(target)) == os.path.normcase(os.path.abspath(source.path)):
        raise SafetyError("Target is the source.")
    is_device = target.lower().startswith("\\\\.\\physicaldrive") or target.startswith("/dev/")
    if is_device:
        require_typed_confirmation(f"ERASE {target}", confirmation)
    if os.path.exists(target) and not is_device and os.path.getsize(target) and confirmation is None:
        raise SafetyError(f"{target} exists; pass a confirmation to overwrite it.")
    bad: list[Range] = []
    with open(target, "r+b" if os.path.exists(target) else "w+b", buffering=0 if is_device else -1) as out:
        pos = 0
        while pos < source.size:
            if cancel and cancel():
                return ImageResult(target, source.size, merge_ranges(bad), False, True, passes_done=1)
            n = min(block, source.size - pos)
            try:
                data = source.read_exact(pos, n)
            except OSError:
                data = bytes(n)
                bad.append((pos, n))
            out.seek(pos)
            out.write(data)
            pos += n
            if progress:
                progress(pos, source.size)
    return ImageResult(target, source.size, merge_ranges(bad), not bad, passes_done=1)
