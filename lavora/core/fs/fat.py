"""Read-only FAT12/16/32 and exFAT parsers with deleted-entry recovery.

Deleted FAT files lose their cluster chain, so recovery assumes the clusters are contiguous from the start cluster
(the classic assumption; fragmented deleted files are better served by signature carving). `overwritten_pct`
reports how many of those clusters are allocated to another file now.
"""
from __future__ import annotations

import struct
from collections.abc import Iterator

from ..device import BlockSource
from .base import FileEntry, Filesystem, FsError, dos_to_dt


def _lfn_checksum(short: bytes) -> int:
    s = 0
    for b in short:
        s = (((s & 1) << 7) + (s >> 1) + b) & 0xFF
    return s


def _lfn_matches(short: bytes, checksum: int, deleted: bool) -> bool:
    """A deleted entry's first byte was overwritten with 0xE5, so try every value it may have had."""
    if not deleted:
        return _lfn_checksum(short) == checksum
    return any(_lfn_checksum(bytes([c]) + short[1:]) == checksum for c in range(256))


class FatFilesystem(Filesystem):
    name = "fat"

    def __init__(self, source: BlockSource):
        super().__init__(source)
        b = source.read_at(0, 512)
        if len(b) < 512 or b[510:512] != b"\x55\xaa":
            raise FsError("FAT boot sector signature missing")
        self.bps, self.spc, self.reserved, self.nfats, self.root_entries = struct.unpack_from("<HBHBH", b, 11)
        total16, _media, fatsz16 = struct.unpack_from("<HBH", b, 19)
        total32, fatsz32 = struct.unpack_from("<II", b, 32)
        if self.bps not in (512, 1024, 2048, 4096) or not self.spc or not self.nfats:
            raise FsError("implausible FAT geometry")
        self.fat_size = fatsz16 or fatsz32
        total = total16 or total32
        root_sectors = (self.root_entries * 32 + self.bps - 1) // self.bps
        self.fat_start = self.reserved * self.bps
        self.root_start = self.fat_start + self.nfats * self.fat_size * self.bps
        self.data_start = self.root_start + root_sectors * self.bps
        self.cluster = self.bps * self.spc
        self.clusters = (total - self.reserved - self.nfats * self.fat_size - root_sectors) // self.spc
        if self.clusters < 4085:
            self.kind, self.eoc = "fat12", 0xFF8
        elif self.clusters < 65525:
            self.kind, self.eoc = "fat16", 0xFFF8
        else:
            self.kind, self.eoc = "fat32", 0x0FFFFFF8
        self.name = self.kind
        self.root_cluster = struct.unpack_from("<I", b, 44)[0] if self.kind == "fat32" else 0
        self._fat_pages: dict[int, bytes] = {}
        self._chains: dict[int, list[int]] = {}

    # ---------------------------------------------------------------- FAT access
    def _fat_bytes(self, off: int, n: int) -> bytes:
        page = off // 4096
        if page not in self._fat_pages:
            if len(self._fat_pages) > 512:
                self._fat_pages.clear()
            self._fat_pages[page] = self.source.read_at(self.fat_start + page * 4096, 4096 + 4)
        base = off - page * 4096
        return self._fat_pages[page][base:base + n]

    def fat_entry(self, n: int) -> int:
        if self.kind == "fat32":
            raw = self._fat_bytes(n * 4, 4)
            return struct.unpack("<I", raw.ljust(4, b"\0"))[0] & 0x0FFFFFFF
        if self.kind == "fat16":
            return struct.unpack("<H", self._fat_bytes(n * 2, 2).ljust(2, b"\0"))[0]
        raw = struct.unpack("<H", self._fat_bytes(n * 3 // 2, 2).ljust(2, b"\0"))[0]
        return raw >> 4 if n & 1 else raw & 0xFFF

    def chain(self, first: int) -> list[int]:
        if first in self._chains:
            return self._chains[first]
        out, seen, n = [], set(), first
        while 2 <= n < self.clusters + 2 and n not in seen:
            out.append(n)
            seen.add(n)
            n = self.fat_entry(n)
            if n >= self.eoc:
                break
        self._chains[first] = out
        return out

    def _cluster_off(self, n: int) -> int:
        return self.data_start + (n - 2) * self.cluster

    # ---------------------------------------------------------------- directories
    def _dir_bytes(self, cluster: int, deleted: bool) -> bytes:
        if cluster == 0:  # FAT12/16 fixed root directory
            return self.source.read_at(self.root_start, self.root_entries * 32)
        if deleted:  # chain was freed: read contiguously until a zero entry shows up (bounded)
            out = b""
            for i in range(64):
                part = self.source.read_at(self._cluster_off(cluster + i), self.cluster)
                out += part
                if len(part) < self.cluster or any(part[j] == 0 for j in range(0, len(part), 32)):
                    break
            return out
        return b"".join(self.source.read_at(self._cluster_off(c), self.cluster) for c in self.chain(cluster))

    def _walk(self, cluster: int, prefix: str, deleted_dir: bool, seen: set[int], include_live: bool) -> Iterator[FileEntry]:
        if cluster in seen:
            return
        seen.add(cluster)
        raw = self._dir_bytes(cluster, deleted_dir)
        lfn: list[tuple[int, str]] = []
        for i in range(0, len(raw) - 31, 32):
            e = raw[i:i + 32]
            if e[0] == 0:
                break
            attr = e[11]
            if attr == 0x0F:
                chars = (e[1:11] + e[14:26] + e[28:32]).decode("utf-16-le", "ignore")
                lfn.append((e[13], chars.split("\0")[0].split("￿")[0]))
                continue
            deleted = e[0] == 0xE5
            if attr & 0x08 or e[:2] in (b". ", b"..") or e[:11] in (b".          ", b"..         "):
                lfn = []
                continue
            short = bytes(e[:11])
            name = self._short_name(short, e[12], deleted)
            if lfn and _lfn_matches(short, lfn[0][0], deleted) and all(c[0] == lfn[0][0] for c in lfn):
                name = "".join(c[1] for c in reversed(lfn))
            lfn = []
            first = (struct.unpack_from("<H", e, 20)[0] << 16 | struct.unpack_from("<H", e, 26)[0]) if self.kind == "fat32" \
                else struct.unpack_from("<H", e, 26)[0]
            size = struct.unpack_from("<I", e, 28)[0]
            is_dir = bool(attr & 0x10)
            path = f"{prefix}/{name}" if prefix else name
            fe = FileEntry(path=path, name=name, size=0 if is_dir else size, is_dir=is_dir, deleted=deleted,
                           mtime=dos_to_dt(struct.unpack_from("<H", e, 24)[0], struct.unpack_from("<H", e, 22)[0]),
                           ctime=dos_to_dt(struct.unpack_from("<H", e, 16)[0], struct.unpack_from("<H", e, 14)[0]),
                           ref=first, fs=self.kind)
            if not is_dir and deleted and size and first >= 2:
                n = (size + self.cluster - 1) // self.cluster
                used = sum(1 for c in range(first, first + n) if c < self.clusters + 2 and self.fat_entry(c) != 0)
                fe.overwritten_pct = 100.0 * used / n
                if first + n > self.clusters + 2:
                    fe.note = "unsupported: start cluster outside the volume"
            if include_live or deleted:
                yield fe
            if is_dir and first >= 2 and (not deleted or self._looks_like_dir(first)):
                yield from self._walk(first, path, deleted or deleted_dir, seen, include_live)

    def _looks_like_dir(self, first: int) -> bool:
        e = self.source.read_at(self._cluster_off(first), 32)
        return len(e) == 32 and e[:11] == b".          " and e[11] & 0x10 == 0x10

    @staticmethod
    def _short_name(short: bytes, nt: int, deleted: bool) -> str:
        base = bytearray(short[:8].rstrip(b" "))
        ext = bytearray(short[8:11].rstrip(b" "))
        if deleted and base:
            base[0] = ord("_")
        elif base and base[0] == 0x05:
            base[0] = 0xE5
        b, x = base.decode("cp437", "replace"), ext.decode("cp437", "replace")
        if nt & 0x08:
            b = b.lower()
        if nt & 0x10:
            x = x.lower()
        return f"{b}.{x}" if x else b

    def scan(self, *, include_live: bool = True, progress=None) -> Iterator[FileEntry]:
        yield from self._walk(self.root_cluster, "", False, set(), include_live)

    def read(self, entry: FileEntry, offset: int = 0, length: int | None = None) -> bytes:
        length = entry.size - offset if length is None else min(length, entry.size - offset)
        if length <= 0 or entry.ref < 2:
            return b""
        n_clusters = (entry.size + self.cluster - 1) // self.cluster
        clusters = list(range(entry.ref, entry.ref + n_clusters)) if entry.deleted else self.chain(entry.ref)
        out = bytearray()
        pos = offset
        while len(out) < length and pos // self.cluster < len(clusters):
            idx, inner = divmod(pos, self.cluster)
            take = min(self.cluster - inner, length - len(out))
            out += self.source.read_at(self._cluster_off(clusters[idx]) + inner, take)
            pos += take
        return bytes(out)


class ExfatFilesystem(Filesystem):
    name = "exfat"

    def __init__(self, source: BlockSource):
        super().__init__(source)
        b = source.read_at(0, 512)
        if b[3:11] != b"EXFAT   ":
            raise FsError("not an exFAT volume")
        self.vol_len = struct.unpack_from("<Q", b, 72)[0]
        self.fat_off, self.fat_len, self.heap_off, self.clusters, self.root = struct.unpack_from("<IIIII", b, 80)
        self.bps = 1 << b[108]
        self.cluster = self.bps << b[109]
        self._bitmap: bytes | None = None

    def _coff(self, n: int) -> int:
        return self.heap_off * self.bps + (n - 2) * self.cluster

    def _fat(self, n: int) -> int:
        return struct.unpack("<I", self.source.read_at(self.fat_off * self.bps + n * 4, 4).ljust(4, b"\0"))[0]

    def _chain(self, first: int, contiguous: bool, size: int) -> list[int]:
        if contiguous:
            return list(range(first, first + (size + self.cluster - 1) // self.cluster))
        out, seen, n = [], set(), first
        while 2 <= n < self.clusters + 2 and n not in seen:
            out.append(n)
            seen.add(n)
            n = self._fat(n)
        return out

    def _dir(self, first: int, contiguous: bool, size: int) -> bytes:
        if contiguous and not size:
            size = self.cluster
        return b"".join(self.source.read_at(self._coff(c), self.cluster) for c in self._chain(first, contiguous, size))

    def _bit(self, n: int) -> bool:
        if self._bitmap is None:
            self._bitmap = b""
            for i in range(0, len(root := self._dir(self.root, False, 0)), 32):
                if root[i] == 0x81:
                    first, size = struct.unpack_from("<IQ", root, i + 20)
                    self._bitmap = b"".join(self.source.read_at(self._coff(c), self.cluster)
                                            for c in self._chain(first, False, size))[:size]
                    break
        k = n - 2
        return k // 8 < len(self._bitmap) and bool(self._bitmap[k // 8] >> (k % 8) & 1)

    def _walk(self, first: int, contiguous: bool, size: int, prefix: str, include_live: bool, seen: set[int]) -> Iterator[FileEntry]:
        if first in seen or first < 2:
            return
        seen.add(first)
        raw = self._dir(first, contiguous, size)
        i = 0
        while i + 32 <= len(raw):
            t = raw[i]
            if t == 0:
                break
            if t in (0x85, 0x05):
                deleted = t == 0x05
                sec = raw[i + 1]
                attrs = struct.unpack_from("<H", raw, i + 4)[0]
                mdate = struct.unpack_from("<I", raw, i + 12)[0]
                cdate = struct.unpack_from("<I", raw, i + 8)[0]
                name, fsize, fcluster, nofat = "", 0, 0, False
                for j in range(1, sec + 1):
                    s = raw[i + 32 * j:i + 32 * j + 32]
                    if len(s) < 32:
                        break
                    if s[0] in (0xC0, 0x40):
                        nofat = bool(s[1] & 2)
                        fsize = struct.unpack_from("<Q", s, 24)[0]
                        fcluster = struct.unpack_from("<I", s, 20)[0]
                    elif s[0] in (0xC1, 0x41):
                        name += s[2:32].decode("utf-16-le", "ignore")
                name = name.split("\0")[0] or "_unnamed"
                is_dir = bool(attrs & 0x10)
                path = f"{prefix}/{name}" if prefix else name
                fe = FileEntry(path=path, name=name, size=0 if is_dir else fsize, is_dir=is_dir, deleted=deleted,
                               mtime=dos_to_dt(mdate >> 16, mdate & 0xFFFF), ctime=dos_to_dt(cdate >> 16, cdate & 0xFFFF),
                               ref=fcluster, fs="exfat", extra={"contiguous": nofat or deleted})
                if deleted and not is_dir and fsize and fcluster >= 2:
                    n = (fsize + self.cluster - 1) // self.cluster
                    fe.overwritten_pct = 100.0 * sum(self._bit(c) for c in range(fcluster, fcluster + n)) / n
                if include_live or deleted:
                    yield fe
                if is_dir:
                    yield from self._walk(fcluster, nofat or deleted, fsize, path, include_live, seen)
                i += 32 * (sec + 1)
            else:
                i += 32

    def scan(self, *, include_live: bool = True, progress=None) -> Iterator[FileEntry]:
        yield from self._walk(self.root, False, 0, "", include_live, set())

    def read(self, entry: FileEntry, offset: int = 0, length: int | None = None) -> bytes:
        length = entry.size - offset if length is None else min(length, entry.size - offset)
        if length <= 0 or entry.ref < 2:
            return b""
        clusters = self._chain(entry.ref, entry.extra.get("contiguous", False), entry.size)
        out = bytearray()
        pos = offset
        while len(out) < length and pos // self.cluster < len(clusters):
            idx, inner = divmod(pos, self.cluster)
            take = min(self.cluster - inner, length - len(out))
            out += self.source.read_at(self._coff(clusters[idx]) + inner, take)
            pos += take
        return bytes(out)
