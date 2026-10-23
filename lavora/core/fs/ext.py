"""Read-only ext2/3/4 parser: live files, plus best-effort recovery of deleted regular files.

How deleted files are found: inodes with a non-zero deletion time are listed; their names are recovered from
directory-entry slack (ext4 merges a removed entry into its predecessor but leaves the bytes behind). Block locations
come from the block map (ext2 keeps it) or from extent bytes left in the inode (ext4 often zeroes only the entry
count). Journal replay is not attempted, so ext3/ext4 results are best-effort and flagged as such.
"""
from __future__ import annotations

import bisect
import struct
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

from ..device import BlockSource
from .base import FileEntry, Filesystem, FsError

EXTENTS_FL = 0x80000
S_IFMT, S_IFREG, S_IFDIR = 0xF000, 0x8000, 0x4000


def _ts(v: int) -> datetime | None:
    try:
        return datetime.fromtimestamp(v, UTC) if v else None
    except (OverflowError, OSError, ValueError):
        return None


class ExtFilesystem(Filesystem):
    name = "ext"

    def __init__(self, source: BlockSource):
        super().__init__(source)
        sb = source.read_at(1024, 1024)
        if len(sb) < 1024 or struct.unpack_from("<H", sb, 56)[0] != 0xEF53:
            raise FsError("ext superblock magic missing")
        self.inodes_count, self.blocks_lo = struct.unpack_from("<II", sb, 0)
        self.first_data_block = struct.unpack_from("<I", sb, 20)[0]
        self.bs = 1024 << struct.unpack_from("<I", sb, 24)[0]
        self.bpg, _fpg, self.ipg = struct.unpack_from("<III", sb, 32)
        rev = struct.unpack_from("<I", sb, 76)[0]
        self.inode_size = struct.unpack_from("<H", sb, 88)[0] if rev >= 1 else 128
        incompat = struct.unpack_from("<I", sb, 96)[0]
        self.is64 = bool(incompat & 0x80)
        self.gd_size = struct.unpack_from("<H", sb, 254)[0] if self.is64 and struct.unpack_from("<H", sb, 254)[0] else 32
        self.blocks = self.blocks_lo | (struct.unpack_from("<I", sb, 336)[0] << 32 if self.is64 else 0)
        if not (self.bpg and self.ipg and self.inode_size >= 128 and self.bs <= 65536):
            raise FsError("implausible ext geometry")
        self.groups = (self.blocks - self.first_data_block + self.bpg - 1) // self.bpg
        gdt = self.source.read_at((self.first_data_block + 1) * self.bs, self.groups * self.gd_size)
        self._gd = []
        for g in range(self.groups):
            d = gdt[g * self.gd_size:(g + 1) * self.gd_size]
            if len(d) < 32:
                break
            bb, ib, it = struct.unpack_from("<III", d, 0)
            if self.gd_size >= 64:
                bb |= struct.unpack_from("<I", d, 32)[0] << 32
                it |= struct.unpack_from("<I", d, 40)[0] << 32
            self._gd.append((bb, it))
        self.has_journal = bool(struct.unpack_from("<I", sb, 92)[0] & 0x4)
        self._bitmaps: dict[int, bytes] = {}
        self._pages: dict[tuple[int, int], bytes] = {}  # inode-table pages (256 inodes) so tree walks do not read per inode
        self._run_cache: tuple[int, list[tuple[int, int, int]]] | None = None

    # ---------------------------------------------------------------- inodes
    def _inode_raw(self, n: int) -> bytes | None:
        g, idx = divmod(n - 1, self.ipg)
        if g >= len(self._gd):
            return None
        page, slot = divmod(idx, 256)
        key = (g, page)
        data = self._pages.get(key)
        if data is None:
            if len(self._pages) > 512:
                self._pages.clear()
            first = page * 256
            count = min(256, self.ipg - first)
            data = self._pages[key] = self.source.read_at(self._gd[g][1] * self.bs + first * self.inode_size, count * self.inode_size)
        raw = data[slot * self.inode_size:(slot + 1) * self.inode_size]
        return raw if len(raw) == self.inode_size else None

    @staticmethod
    def _fields(raw: bytes) -> dict:
        mode, _uid, size_lo, _at, ctime, mtime, dtime = struct.unpack_from("<HHIIIII", raw, 0)
        links, _blks, flags = struct.unpack_from("<HII", raw, 26)
        size = size_lo | (struct.unpack_from("<I", raw, 108)[0] << 32 if mode & S_IFMT == S_IFREG else 0)
        return {"mode": mode, "size": size, "ctime": ctime, "mtime": mtime, "dtime": dtime, "links": links,
                "flags": flags, "iblock": raw[40:100]}

    def _extents(self, f: dict, best_effort: bool) -> list[tuple[int, int, int]]:
        """(logical block, physical block, length) runs for an inode."""
        if f["flags"] & EXTENTS_FL:
            return self._extent_tree(f["iblock"], best_effort)
        return self._block_map(f["iblock"])

    def _extent_tree(self, node: bytes, best_effort: bool, depth_guard: int = 0) -> list[tuple[int, int, int]]:
        magic, entries, _max, depth = struct.unpack_from("<HHHH", node, 0)
        if magic != 0xF30A or depth_guard > 5:
            return []
        if entries == 0 and best_effort:  # ext4 zeroes the count but leaves the extent bytes in place
            entries = (len(node) - 12) // 12
        out: list[tuple[int, int, int]] = []
        for i in range(entries):
            e = node[12 + 12 * i:24 + 12 * i]
            if len(e) < 12:
                break
            if depth == 0:
                lblk, ln, hi, lo = struct.unpack_from("<IHHI", e, 0)
                ln = ln - 32768 if ln > 32768 else ln
                pblk = (hi << 32) | lo
                if ln == 0 or pblk == 0 or pblk + ln > self.blocks or (out and lblk < out[-1][0]):
                    if best_effort:
                        break
                    continue
                out.append((lblk, pblk, ln))
            else:
                _lblk, lo, hi = struct.unpack_from("<IIH", e, 0)
                child = self.source.read_at(((hi << 32) | lo) * self.bs, self.bs)
                out += self._extent_tree(child, best_effort, depth_guard + 1)
        return out

    def _block_map(self, iblock: bytes) -> list[tuple[int, int, int]]:
        ptrs = list(struct.unpack("<15I", iblock))
        blocks: list[int] = ptrs[:12]
        per = self.bs // 4

        def indirect(b: int, level: int) -> list[int]:
            if b == 0 or b >= self.blocks:
                return [0] * (per ** level)
            arr = struct.unpack(f"<{per}I", self.source.read_at(b * self.bs, self.bs).ljust(self.bs, b"\0"))
            if level == 1:
                return list(arr)
            out: list[int] = []
            for x in arr:
                out += indirect(x, level - 1)
            return out

        for lvl, p in enumerate(ptrs[12:15], start=1):
            if p:
                blocks += indirect(p, lvl)
            else:
                break
        runs: list[tuple[int, int, int]] = []
        for lblk, p in enumerate(blocks):
            if p == 0 or p >= self.blocks:
                continue
            if runs and runs[-1][0] + runs[-1][2] == lblk and runs[-1][1] + runs[-1][2] == p:
                runs[-1] = (runs[-1][0], runs[-1][1], runs[-1][2] + 1)
            else:
                runs.append((lblk, p, 1))
        return runs

    def _read_runs(self, runs: list[tuple[int, int, int]], size: int, offset: int, length: int) -> bytes:
        length = max(0, min(length, size - offset))
        out = bytearray(length)
        if not length or not runs:
            return bytes(out)
        ordered = all(runs[i][0] <= runs[i + 1][0] for i in range(len(runs) - 1))
        if not ordered:
            runs = sorted(runs)
        starts = [r[0] * self.bs for r in runs]
        i = max(0, bisect.bisect_right(starts, offset) - 1)
        while i < len(runs) and starts[i] < offset + length:
            lblk, pblk, ln = runs[i]
            start, end = lblk * self.bs, (lblk + ln) * self.bs
            lo, hi = max(start, offset), min(end, offset + length)
            if lo < hi:
                out[lo - offset:hi - offset] = self.source.read_at(pblk * self.bs + (lo - start), hi - lo)
            i += 1
        return bytes(out)

    def _runs_allocated_pct(self, runs: list[tuple[int, int, int]], limit: int = 4096) -> float:
        """Share of the file's blocks that are allocated now (exact when small, a 4096-point sample for huge files)."""
        total = sum(ln for _l, _p, ln in runs)
        if not total:
            return 0.0
        step = max(1, total // limit)
        hit = seen = 0
        idx = 0
        for _l, p, ln in runs:
            first = (-idx) % step  # keep a global sampling stride across runs
            for k in range(first, ln, step):
                seen += 1
                hit += self._block_allocated(p + k)
            idx += ln
        return 100.0 * hit / seen if seen else 0.0

    def _block_allocated(self, b: int) -> bool:
        g, idx = divmod(b - self.first_data_block, self.bpg)
        if g >= len(self._gd):
            return True
        if g not in self._bitmaps:
            self._bitmaps[g] = self.source.read_at(self._gd[g][0] * self.bs, self.bs)
        bm = self._bitmaps[g]
        return idx // 8 < len(bm) and bool(bm[idx // 8] >> (idx % 8) & 1)

    # ---------------------------------------------------------------- directories
    @staticmethod
    def _entries(block: bytes, slack: bool) -> Iterator[tuple[int, str, bool]]:
        """Yield (inode, name, hidden) from a directory block; hidden ones come from slack space."""
        pos = 0
        while pos + 8 <= len(block):
            ino, rec_len, name_len, _ftype = struct.unpack_from("<IHBB", block, pos)
            if rec_len < 8 or rec_len % 4 or pos + rec_len > len(block):
                break
            actual = 8 + ((name_len + 3) & ~3)
            if ino and name_len and actual <= rec_len:
                yield ino, block[pos + 8:pos + 8 + name_len].decode("utf-8", "replace"), False
            if slack and rec_len > actual + 8 and (ino or pos == 0):
                hp, end = pos + actual, pos + rec_len
                while hp + 8 <= end:
                    h_ino, h_len, h_nl, _ft = struct.unpack_from("<IHBB", block, hp)
                    h_actual = 8 + ((h_nl + 3) & ~3)
                    if h_len < 8 or h_len % 4 or hp + h_len > end or h_actual > h_len:
                        break
                    if h_ino and h_nl and h_ino < 2 ** 31:
                        yield h_ino, block[hp + 8:hp + 8 + h_nl].decode("utf-8", "replace"), True
                    hp += h_len
            pos += rec_len

    def _dir_blocks(self, n: int) -> list[bytes]:
        raw = self._inode_raw(n)
        if raw is None:
            return []
        f = self._fields(raw)
        data = self._read_runs(self._extents(f, False), f["size"], 0, f["size"])
        return [data[i:i + self.bs] for i in range(0, len(data), self.bs)]

    def scan(self, *, include_live: bool = True, progress=None, cancel: Callable[[], bool] | None = None) -> Iterator[FileEntry]:
        live: dict[int, str] = {2: ""}  # directories only: files do not need to be remembered
        names: dict[int, tuple[int, str]] = {}  # deleted-name candidates: inode -> (parent inode, name)
        stack = [2]
        while stack:
            if cancel and cancel():
                return
            d = stack.pop()
            for blk in self._dir_blocks(d):
                for ino, name, hidden in self._entries(blk, True):
                    if name in (".", ".."):
                        continue
                    if hidden:
                        if len(names) < 5_000_000:
                            names.setdefault(ino, (d, name))
                        continue
                    raw = self._inode_raw(ino)
                    if raw is None:
                        continue
                    f = self._fields(raw)
                    path = f"{live[d]}/{name}" if live[d] else name
                    is_dir = f["mode"] & S_IFMT == S_IFDIR
                    if is_dir:
                        if ino in live:
                            continue
                        live[ino] = path
                        stack.append(ino)
                    if include_live and f["mode"] & S_IFMT in (S_IFREG, S_IFDIR):
                        yield FileEntry(path, name, 0 if is_dir else f["size"], is_dir, False, _ts(f["mtime"]),
                                        _ts(f["ctime"]), ino, "ext")
        total = self.ipg * len(self._gd)
        for n in range(1, total + 1):
            if cancel and n % 4096 == 0 and cancel():
                return
            raw = self._inode_raw(n)
            if raw is None or not raw[20] | raw[21] | raw[22] | raw[23]:  # dtime == 0: not deleted (the common case, skipped cheaply)
                if progress and n % 65536 == 0:
                    progress(n, total)
                continue
            f = self._fields(raw)
            if not (f["mode"] & S_IFMT == S_IFREG and f["size"]):
                continue
            parent, name = names.get(n, (0, f"inode-{n}"))
            base = live.get(parent, "$OrphanFiles")
            fe = FileEntry(f"{base}/{name}" if base else name, name, f["size"], False, True, _ts(f["mtime"]),
                           _ts(f["ctime"]), n, "ext")
            runs = self._extents(f, True)
            if not runs:
                fe.note = "unsupported: block locations were cleared on delete (journal replay not implemented)"
            else:
                fe.overwritten_pct = self._runs_allocated_pct(runs)
                if self.has_journal:
                    fe.note = "best effort (journalled filesystem)"
            yield fe

    def byte_ranges(self, entry: FileEntry) -> list[tuple[int, int]] | None:
        raw = self._inode_raw(entry.ref)
        if raw is None:
            return []
        return [(p * self.bs, ln * self.bs) for _l, p, ln in self._extents(self._fields(raw), entry.deleted)]

    def read(self, entry: FileEntry, offset: int = 0, length: int | None = None) -> bytes:
        raw = self._inode_raw(entry.ref)
        if raw is None:
            raise FsError(f"inode {entry.ref} unreadable")
        f = self._fields(raw)
        length = entry.size - offset if length is None else length
        if self._run_cache is None or self._run_cache[0] != entry.ref:  # streamed in 1 MiB steps: parse the extent tree once
            self._run_cache = (entry.ref, self._extents(f, entry.deleted))
        return self._read_runs(self._run_cache[1], min(f["size"], entry.size), offset, length)
