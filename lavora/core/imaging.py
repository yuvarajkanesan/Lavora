"""Sector-by-sector imaging with a ddrescue-style bad-sector map, pause/resume, compression, hashing and cloning.

Pass 1 copies forward in large blocks and skips anything that errors. Pass 2 re-reads the skipped blocks in small
steps and down to single sectors. Pass 3 retries the remaining bad sectors (in reverse order). Progress is kept in a
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
            if self.state["pass"] == 2 and self.passes >= 2 and not self._pass_retry(out, 2, self.state["suspect"], 4096, progress):
                return self._result(False, True)
            for p in range(3, self.passes + 1):
                if self.state["pass"] == p and not self._pass_retry(out, p, self.state["bad"], SECTOR, progress,
                                                                    reverse=True):
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

    def _pass1(self, out, progress) -> bool:
        pos = self.state["next"]
        size = self.source.size
        suspect = [tuple(r) for r in self.state["suspect"]]
        n = 0
        while pos < size:
            if self._checkpoint():
                self.state["suspect"] = [list(r) for r in suspect]
                self.state["next"] = pos
                self._save()
                return False
            ln = min(self.block, size - pos)
            self.pos = pos
            t0 = time.perf_counter()
            try:
                data = self.source.read_exact(pos, ln)
                out.seek(pos)
                out.write(data)
            except OSError:
                suspect.append((pos, ln))
            self.last_read_s = time.perf_counter() - t0
            pos += ln
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

    def _pass_retry(self, out, pass_no: int, todo: list, step: int, progress, reverse: bool = False) -> bool:
        ranges = [tuple(r) for r in todo]
        still_bad: list[Range] = []
        ordered = sorted(ranges, reverse=reverse)
        for idx, (off, ln) in enumerate(ordered):
            chunks = list(range(off, off + ln, step))
            if reverse:
                chunks.reverse()
            for c in chunks:
                if self._checkpoint():
                    # keep what is not yet retried
                    rest = still_bad + [(o, n) for o, n in ordered[idx:]]
                    self._store(pass_no, rest)
                    return False
                n = min(step, off + ln - c)
                try:
                    data = self.source.read_exact(c, n)
                    out.seek(c)
                    out.write(data)
                except OSError:
                    if step > SECTOR:  # fall back to sector granularity
                        for s in range(c, c + n, SECTOR):
                            sn = min(SECTOR, c + n - s)
                            try:
                                data = self.source.read_exact(s, sn)
                                out.seek(s)
                                out.write(data)
                            except OSError:
                                still_bad.append((s, sn))
                    else:
                        still_bad.append((c, n))
            if progress:
                progress(off + ln, self.source.size)
        self.state["suspect"] = []
        self.state["bad"] = [list(r) for r in merge_ranges(still_bad)]
        self.state["pass"] = pass_no + 1
        self._save()
        return True

    def _store(self, pass_no: int, rest: list[Range]) -> None:
        key = "suspect" if pass_no == 2 else "bad"
        self.state[key] = [list(r) for r in merge_ranges(rest)]
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
