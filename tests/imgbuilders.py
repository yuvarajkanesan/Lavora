"""Pure-Python disk-image writers for the test suite (no admin rights, no physical disks, no VHD tools needed).

Each builder lays out a small but structurally real filesystem so the parsers are exercised against the on-disk formats:
FAT32 (with LFN entries), exFAT, NTFS (MFT with fixups, resident + non-resident + sparse data) and ext2/ext4.
`delete()` mimics what the OS does when a file is deleted (flags cleared, chains/bitmaps freed, data left in place).
"""
from __future__ import annotations

import struct
import time

SECTOR = 512
EPOCH_FT = 116444736000000000  # 1970-01-01 as FILETIME


def filetime(ts: float) -> int:
    return int(ts * 10_000_000) + EPOCH_FT


def dos_dt(ts: float) -> tuple[int, int]:
    t = time.gmtime(ts)
    return ((t.tm_year - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday, (t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2)


# ====================================================================== FAT32
def _lfn_checksum(short: bytes) -> int:
    s = 0
    for b in short:
        s = (((s & 1) << 7) + (s >> 1) + b) & 0xFF
    return s


class Fat32Builder:
    def __init__(self, total_sectors: int = 70000, spc: int = 1):
        self.bps, self.spc, self.reserved, self.nfats = SECTOR, spc, 32, 2
        self.total = total_sectors
        clusters = (total_sectors - 32) // spc
        fat_sectors = 0
        for _ in range(5):
            fat_sectors = ((clusters + 2) * 4 + SECTOR - 1) // SECTOR
            clusters = (total_sectors - 32 - 2 * fat_sectors) // spc
        self.fat_sectors, self.clusters = fat_sectors, clusters
        assert clusters >= 65525, "FAT32 needs at least 65525 clusters"
        self.img = bytearray(total_sectors * SECTOR)
        self.data_start = (32 + 2 * fat_sectors) * SECTOR
        self.cs = SECTOR * spc
        self.fat = [0] * (clusters + 2)
        self.fat[0], self.fat[1] = 0x0FFFFFF8, 0x0FFFFFFF
        self.fat[2] = 0x0FFFFFFF
        self.dirs: dict[str, list[int]] = {"": [2]}
        self.dir_count: dict[str, int] = {"": 0}
        self.files: dict[str, tuple[str, int, int, int]] = {}  # path -> (dir, first idx, entries, first cluster)
        self._boot()

    def _boot(self) -> None:
        b = bytearray(SECTOR)
        b[0:3] = b"\xeb\x58\x90"
        b[3:11] = b"MSDOS5.0"
        struct.pack_into("<HBHBHHBHHHII", b, 11, SECTOR, self.spc, 32, 2, 0, 0, 0xF8, 0, 63, 255, 0, self.total)
        struct.pack_into("<IHHIH", b, 36, self.fat_sectors, 0, 0, 2, 1)
        struct.pack_into("<H", b, 50, 6)
        b[64], b[66] = 0x80, 0x29
        struct.pack_into("<I", b, 67, 0x1234ABCD)
        b[71:82] = b"LAVORATEST "
        b[82:90] = b"FAT32   "
        b[510:512] = b"\x55\xaa"
        self.img[0:SECTOR] = b
        self.img[6 * SECTOR:7 * SECTOR] = b

    def _coff(self, c: int) -> int:
        return self.data_start + (c - 2) * self.cs

    def _alloc(self, n: int) -> list[int]:
        run, start = 0, 3
        for c in range(3, self.clusters + 2):
            if self.fat[c] == 0:
                if run == 0:
                    start = c
                run += 1
                if run == n:
                    return list(range(start, start + n))
            else:
                run = 0
        raise RuntimeError("image full")

    def _link(self, clusters: list[int]) -> None:
        for a, b in zip(clusters, clusters[1:]):
            self.fat[a] = b
        self.fat[clusters[-1]] = 0x0FFFFFFF

    def _dir_slot(self, d: str, idx: int) -> int:
        per = self.cs // 32
        while idx // per >= len(self.dirs[d]):
            c = self._alloc(1)[0]
            self.fat[self.dirs[d][-1]] = c
            self.fat[c] = 0x0FFFFFFF
            self.img[self._coff(c):self._coff(c) + self.cs] = bytes(self.cs)
            self.dirs[d].append(c)
        return self._coff(self.dirs[d][idx // per]) + (idx % per) * 32

    @staticmethod
    def _simple(name: str) -> bool:
        base, _, ext = name.rpartition(".") if "." in name else (name, "", "")
        return len(base) <= 8 and len(ext) <= 3 and name == name.upper() and name.isascii() and " " not in name

    def _short(self, name: str, n: int) -> bytes:
        base, _, ext = name.rpartition(".") if "." in name else (name, "", "")
        if self._simple(name):
            return (base.ljust(8) + ext.ljust(3)).encode()
        stem = "".join(c for c in base.upper() if c.isalnum())[:6] or "FILE"
        return f"{stem}~{n}".ljust(8).encode() + ext.upper()[:3].ljust(3).encode()

    def _add_entry(self, d: str, name: str, attr: int, first: int, size: int, ts: float) -> tuple[int, int]:
        short = self._short(name, self.dir_count[d] + 1)
        entries = []
        if not self._simple(name):
            padded = name + chr(0) + chr(0xFFFF) * ((13 - (len(name) + 1) % 13) % 13)
            units = [padded[i:i + 13] for i in range(0, len(padded), 13)]
            cks = _lfn_checksum(short)
            for seq in range(len(units), 0, -1):
                u = units[seq - 1].encode("utf-16-le")
                e = bytearray(32)
                e[0] = seq | (0x40 if seq == len(units) else 0)
                e[1:11], e[11], e[13], e[14:26], e[28:32] = u[:10], 0x0F, cks, u[10:22], u[22:26]
                entries.append(bytes(e))
        e = bytearray(32)
        e[0:11] = short
        e[11] = attr
        cd, ct = dos_dt(ts)
        struct.pack_into("<HH", e, 14, ct, cd)
        struct.pack_into("<H", e, 20, first >> 16)
        struct.pack_into("<HH", e, 22, ct, cd)
        struct.pack_into("<HI", e, 26, first & 0xFFFF, size)
        entries.append(bytes(e))
        start = self.dir_count[d]
        for i, ent in enumerate(entries):
            off = self._dir_slot(d, start + i)
            self.img[off:off + 32] = ent
        self.dir_count[d] += len(entries)
        return start, len(entries)

    def mkdir(self, path: str, ts: float = 1_700_000_000) -> None:
        parent, _, name = path.rpartition("/")
        if parent and parent not in self.dirs:
            self.mkdir(parent, ts)
        c = self._alloc(1)[0]
        self._link([c])
        self.dirs[path], self.dir_count[path] = [c], 0
        self.img[self._coff(c):self._coff(c) + self.cs] = bytes(self.cs)
        idx, n = self._add_entry(parent, name, 0x10, c, 0, ts)
        self.files[path] = (parent, idx, n, c)
        for nm, tgt in ((".", c), ("..", self.dirs[parent][0] if parent else 0)):
            off = self._dir_slot(path, self.dir_count[path])
            e = bytearray(32)
            e[0:11] = nm.ljust(11).encode()
            e[11] = 0x10
            struct.pack_into("<H", e, 20, tgt >> 16)
            struct.pack_into("<H", e, 26, tgt & 0xFFFF)
            self.img[off:off + 32] = e
            self.dir_count[path] += 1

    def add_file(self, path: str, data: bytes, ts: float = 1_700_000_000) -> int:
        parent, _, name = path.rpartition("/")
        if parent and parent not in self.dirs:
            self.mkdir(parent)
        n = max(1, (len(data) + self.cs - 1) // self.cs) if data else 0
        clusters = self._alloc(n) if n else []
        if clusters:
            self._link(clusters)
            padded = data.ljust(n * self.cs, b"\0")
            for i, c in enumerate(clusters):
                self.img[self._coff(c):self._coff(c) + self.cs] = padded[i * self.cs:(i + 1) * self.cs]
        first = clusters[0] if clusters else 0
        idx, cnt = self._add_entry(parent, name, 0x20, first, len(data), ts)
        self.files[path] = (parent, idx, cnt, first)
        return first

    def delete(self, path: str) -> None:
        d, idx, cnt, first = self.files.pop(path)
        for i in range(cnt):
            off = self._dir_slot(d, idx + i)
            self.img[off] = 0xE5
        c = first
        while c >= 2 and c < len(self.fat) and self.fat[c] != 0:
            nxt = self.fat[c]
            self.fat[c] = 0
            c = nxt if nxt < 0x0FFFFFF8 else 0

    def build(self) -> bytes:
        raw = b"".join(struct.pack("<I", v) for v in self.fat)
        for k in range(2):
            off = (32 + k * self.fat_sectors) * SECTOR
            self.img[off:off + len(raw)] = raw
        return bytes(self.img)


# ====================================================================== exFAT
class ExfatBuilder:
    def __init__(self, total_sectors: int = 40000):
        self.total = total_sectors
        self.heap_off = 128
        self.spc_shift = 3
        self.cs = SECTOR << 3
        self.clusters = (total_sectors - self.heap_off) >> 3
        self.fat_off, self.fat_len = 24, (self.clusters + 2) * 4 // SECTOR + 1
        self.img = bytearray(total_sectors * SECTOR)
        self.fat = [0] * (self.clusters + 2)
        self.fat[0], self.fat[1] = 0xFFFFFFF8, 0xFFFFFFFF
        self.bitmap = bytearray((self.clusters + 7) // 8)
        for c in (2, 3, 4):  # bitmap, upcase, root
            self._mark(c, True)
            self.fat[c] = 0xFFFFFFFF
        self.root_entries: list[bytes] = []
        self.dirs: dict[str, list[bytes]] = {"": self.root_entries}
        self.dir_cluster: dict[str, int] = {"": 4}
        self.index: dict[str, tuple[str, int, int]] = {}  # path -> (dir, idx, count)
        self.sizes: dict[str, tuple[int, int]] = {}

    def _mark(self, c: int, on: bool) -> None:
        k = c - 2
        if on:
            self.bitmap[k // 8] |= 1 << (k % 8)
        else:
            self.bitmap[k // 8] &= ~(1 << (k % 8)) & 0xFF

    def _alloc(self, n: int) -> list[int]:
        run, start = 0, 5
        for c in range(5, self.clusters + 2):
            if not self.bitmap[(c - 2) // 8] >> ((c - 2) % 8) & 1:
                if run == 0:
                    start = c
                run += 1
                if run == n:
                    return list(range(start, start + n))
            else:
                run = 0
        raise RuntimeError("image full")

    def _coff(self, c: int) -> int:
        return (self.heap_off * SECTOR) + (c - 2) * self.cs

    def _entry_set(self, name: str, attrs: int, first: int, size: int, nofat: bool, ts: float) -> list[bytes]:
        d, t = dos_dt(ts)
        stamp = (d << 16) | t
        units = [name[i:i + 15] for i in range(0, len(name), 15)] or [""]
        primary = bytearray(32)
        primary[0], primary[1] = 0x85, 1 + len(units)
        struct.pack_into("<HHII", primary, 4, attrs, 0, stamp, stamp)
        stream = bytearray(32)
        stream[0], stream[1], stream[3] = 0xC0, 0x01 | (0x02 if nofat else 0), len(name)
        struct.pack_into("<Q", stream, 8, size)
        struct.pack_into("<I", stream, 20, first)
        struct.pack_into("<Q", stream, 24, size)
        out = [bytes(primary), bytes(stream)]
        for u in units:
            e = bytearray(32)
            e[0] = 0xC1
            e[2:32] = u.encode("utf-16-le").ljust(30, b"\0")
            out.append(bytes(e))
        return out

    def mkdir(self, path: str, ts: float = 1_700_000_000) -> None:
        parent, _, name = path.rpartition("/")
        if parent and parent not in self.dirs:
            self.mkdir(parent, ts)
        c = self._alloc(1)[0]
        self._mark(c, True)
        self.fat[c] = 0xFFFFFFFF
        self.dirs[path], self.dir_cluster[path] = [], c
        ents = self._entry_set(name, 0x10, c, self.cs, False, ts)
        self.index[path] = (parent, len(self.dirs[parent]), len(ents))
        self.dirs[parent] += ents

    def add_file(self, path: str, data: bytes, *, nofat: bool = True, ts: float = 1_700_000_000) -> int:
        parent, _, name = path.rpartition("/")
        if parent and parent not in self.dirs:
            self.mkdir(parent)
        n = (len(data) + self.cs - 1) // self.cs
        clusters = self._alloc(n) if n else []
        padded = data.ljust(n * self.cs, b"\0")
        for i, c in enumerate(clusters):
            self._mark(c, True)
            self.img[self._coff(c):self._coff(c) + self.cs] = padded[i * self.cs:(i + 1) * self.cs]
            if not nofat:
                self.fat[c] = clusters[i + 1] if i + 1 < len(clusters) else 0xFFFFFFFF
        first = clusters[0] if clusters else 0
        ents = self._entry_set(name, 0x20, first, len(data), nofat, ts)
        self.index[path] = (parent, len(self.dirs[parent]), len(ents))
        self.dirs[parent] += ents
        self.sizes[path] = (first, n)
        return first

    def delete(self, path: str) -> None:
        d, idx, cnt = self.index.pop(path)
        for i in range(cnt):
            e = bytearray(self.dirs[d][idx + i])
            e[0] &= 0x7F
            self.dirs[d][idx + i] = bytes(e)
        first, n = self.sizes.pop(path)
        for c in range(first, first + n):
            self._mark(c, False)

    def build(self) -> bytes:
        b = bytearray(SECTOR)
        b[0:3] = b"\xeb\x76\x90"
        b[3:11] = b"EXFAT   "
        struct.pack_into("<Q", b, 64, 0)
        struct.pack_into("<Q", b, 72, self.total)
        struct.pack_into("<IIIII", b, 80, self.fat_off, self.fat_len, self.heap_off, self.clusters, 4)
        struct.pack_into("<I", b, 100, 0xCAFEBABE)
        struct.pack_into("<H", b, 104, 0x0100)
        b[108], b[109], b[110], b[111] = 9, 3, 1, 0x80
        b[510:512] = b"\x55\xaa"
        self.img[0:SECTOR] = b
        self.img[12 * SECTOR:13 * SECTOR] = b  # exFAT keeps a backup boot region at sectors 12-23
        raw = b"".join(struct.pack("<I", v) for v in self.fat)
        self.img[self.fat_off * SECTOR:self.fat_off * SECTOR + len(raw)] = raw
        self.img[self._coff(2):self._coff(2) + len(self.bitmap)] = self.bitmap
        self.img[self._coff(3):self._coff(3) + 16] = bytes(16)
        root = bytearray()
        bm = bytearray(32)
        bm[0] = 0x81
        struct.pack_into("<IQ", bm, 20, 2, len(self.bitmap))
        root += bm
        for e in self.root_entries:
            root += e
        self.img[self._coff(4):self._coff(4) + len(root)] = root
        for path, ents in self.dirs.items():
            if path:
                blob = b"".join(ents)
                c = self.dir_cluster[path]
                self.img[self._coff(c):self._coff(c) + len(blob)] = blob
        return bytes(self.img)


# ====================================================================== NTFS
class NtfsBuilder:
    BPS, SPC, REC = 512, 8, 1024

    def __init__(self, total_sectors: int = 20480, mft_records: int = 64):
        self.total = total_sectors
        self.cs = self.BPS * self.SPC
        self.clusters = (total_sectors - 1) // self.SPC
        self.img = bytearray(total_sectors * self.BPS)
        self.mft_lcn = 4
        self.mft_records = mft_records
        self.mft_clusters = mft_records * self.REC // self.cs
        self.bitmap_lcn = self.mft_lcn + self.mft_clusters
        self.bitmap = bytearray((self.clusters + 7) // 8)
        for c in range(0, self.bitmap_lcn + 1):
            self._set(c, True)
        self.records: dict[int, bytes] = {}
        self.next_rec = 16
        self.paths: dict[str, int] = {"": 5}
        self.sizes: dict[str, tuple[int, list]] = {}
        self._metadata()

    # ---- bitmap / allocation
    def _set(self, c: int, on: bool) -> None:
        if on:
            self.bitmap[c // 8] |= 1 << (c % 8)
        else:
            self.bitmap[c // 8] &= ~(1 << (c % 8)) & 0xFF

    def _free(self, c: int) -> bool:
        return not self.bitmap[c // 8] >> (c % 8) & 1

    def alloc(self, n: int) -> int:
        run, start = 0, 0
        for c in range(self.bitmap_lcn + 1, self.clusters):
            if self._free(c):
                if run == 0:
                    start = c
                run += 1
                if run == n:
                    for k in range(start, start + n):
                        self._set(k, True)
                    return start
            else:
                run = 0
        raise RuntimeError("image full")

    # ---- attribute encoders
    @staticmethod
    def _pad8(b: bytes) -> bytes:
        return b + bytes(-len(b) % 8)

    def _resident(self, atype: int, content: bytes, aid: int, name: str = "", flags: int = 0) -> bytes:
        nm = name.encode("utf-16-le")
        hdr = struct.pack("<IIBBHHH", atype, 0, 0, len(name), 24, flags, aid)
        body = struct.pack("<IHH", len(content), 24 + len(nm), 0)
        raw = hdr + body + nm
        raw = self._pad8(raw + content)
        return struct.pack("<II", atype, len(raw)) + raw[8:]

    def _runs(self, runs: list[tuple[int | None, int]]) -> bytes:
        out, prev = b"", 0
        for lcn, ln in runs:
            lb = ln.to_bytes(max(1, (ln.bit_length() + 7) // 8), "little")
            if lcn is None:
                out += bytes([len(lb)]) + lb
                continue
            delta = lcn - prev
            ob = delta.to_bytes(max(1, (delta.bit_length() + 8) // 8), "little", signed=True)
            out += bytes([len(ob) << 4 | len(lb)]) + lb + ob
            prev = lcn
        return out + b"\x00"

    def _nonres(self, runs: list[tuple[int | None, int]], real: int, aid: int, flags: int = 0, init: int | None = None) -> bytes:
        rl = self._runs(runs)
        clusters = sum(ln for _l, ln in runs)
        hdr = struct.pack("<IIBBHHH", 0x80, 0, 1, 0, 64, flags, aid)
        body = struct.pack("<QQHHIQQQ", 0, max(0, clusters - 1), 64, 0, 0, clusters * self.cs, real,
                           real if init is None else init)
        raw = self._pad8(hdr + body + rl)
        return struct.pack("<II", 0x80, len(raw)) + raw[8:]

    def _filename(self, parent: int, name: str, size: int, is_dir: bool, ts: float) -> bytes:
        ft = filetime(ts)
        nm = name.encode("utf-16-le")
        content = struct.pack("<QQQQQQQII", parent | (1 << 48), ft, ft, ft, ft, 0, size, 0x10000000 if is_dir else 0x20, 0)
        content += bytes([len(name), 1]) + nm
        return self._resident(0x30, content, 1)

    def _stdinfo(self, ts: float) -> bytes:
        ft = filetime(ts)
        return self._resident(0x10, struct.pack("<QQQQI", ft, ft, ft, ft, 0x20) + bytes(28), 0)

    def _record(self, number: int, flags: int, attrs: list[bytes]) -> bytes:
        rec = bytearray(self.REC)
        rec[0:4] = b"FILE"
        struct.pack_into("<HH", rec, 4, 48, self.REC // 512 + 1)
        struct.pack_into("<HHH", rec, 16, 1, 1, 56)
        struct.pack_into("<H", rec, 22, flags)
        pos = 56
        for a in attrs:
            rec[pos:pos + len(a)] = a
            pos += len(a)
        rec[pos:pos + 4] = b"\xff\xff\xff\xff"
        struct.pack_into("<II", rec, 24, pos + 8, self.REC)
        struct.pack_into("<I", rec, 44, number)
        usn = b"\x01\x00"
        rec[48:50] = usn
        for i in range(1, self.REC // 512 + 1):
            end = i * 512 - 2
            rec[48 + 2 * i:50 + 2 * i] = rec[end:end + 2]
            rec[end:end + 2] = usn
        return bytes(rec)

    def _metadata(self) -> None:
        mft_runs = [(self.mft_lcn, self.mft_clusters)]
        self.records[0] = self._record(0, 1, [self._stdinfo(0), self._filename(5, "$MFT", self.mft_records * self.REC, False, 0),
                                              self._nonres(mft_runs, self.mft_records * self.REC, 2)])
        self.records[5] = self._record(5, 3, [self._stdinfo(0), self._filename(5, ".", 0, True, 0)])
        for n, nm in ((1, "$MFTMirr"), (2, "$LogFile"), (3, "$Volume"), (4, "$AttrDef"), (7, "$Boot"), (8, "$BadClus"),
                      (9, "$Secure"), (10, "$UpCase"), (11, "$Extend")):
            self.records[n] = self._record(n, 1, [self._filename(5, nm, 0, False, 0)])

    # ---- public API
    def mkdir(self, path: str, ts: float = 1_700_000_000) -> int:
        parent, _, name = path.rpartition("/")
        if parent and parent not in self.paths:
            self.mkdir(parent, ts)
        n = self.next_rec
        self.next_rec += 1
        self.paths[path] = n
        self.records[n] = self._record(n, 3, [self._stdinfo(ts), self._filename(self.paths[parent], name, 0, True, ts)])
        return n

    def add_file(self, path: str, data: bytes, ts: float = 1_700_000_000, *, fragments: int = 1, flags: int = 0) -> int:
        parent, _, name = path.rpartition("/")
        if parent and parent not in self.paths:
            self.mkdir(parent, ts)
        n = self.next_rec
        self.next_rec += 1
        if n >= self.mft_records:
            raise RuntimeError("MFT full")
        runs: list[tuple[int | None, int]] = []
        if len(data) <= 600 and flags == 0:
            attrs = [self._stdinfo(ts), self._filename(self.paths[parent], name, len(data), False, ts),
                     self._resident(0x80, data, 2)]
        else:
            total = (len(data) + self.cs - 1) // self.cs
            pieces = [total // fragments + (1 if i < total % fragments else 0) for i in range(fragments)]
            blob = data.ljust(total * self.cs, b"\0")
            pos = 0
            for i, pc in enumerate(pieces):
                if i + 1 < len(pieces):  # force fragmentation: leave a one-cluster gap after each piece
                    lcn = self.alloc(pc)
                    self.alloc(1)
                else:
                    lcn = self.alloc(pc)
                self.img[lcn * self.cs:(lcn + pc) * self.cs] = blob[pos * self.cs:(pos + pc) * self.cs]
                runs.append((lcn, pc))
                pos += pc
            attrs = [self._stdinfo(ts), self._filename(self.paths[parent], name, len(data), False, ts),
                     self._nonres(runs, len(data), 2, flags)]
        self.records[n] = self._record(n, 1, attrs)
        self.paths[path] = n
        self.sizes[path] = (n, runs)
        return n

    def add_sparse(self, path: str, head: bytes, hole_clusters: int, tail: bytes, ts: float = 1_700_000_000) -> int:
        parent, _, name = path.rpartition("/")
        n = self.next_rec
        self.next_rec += 1
        hc, tc = (len(head) + self.cs - 1) // self.cs, (len(tail) + self.cs - 1) // self.cs
        a, b = self.alloc(hc), self.alloc(tc)
        self.img[a * self.cs:a * self.cs + len(head)] = head
        self.img[b * self.cs:b * self.cs + len(tail)] = tail
        runs = [(a, hc), (None, hole_clusters), (b, tc)]
        real = (hc + hole_clusters + tc) * self.cs
        self.records[n] = self._record(n, 1, [self._stdinfo(ts), self._filename(self.paths[parent], name, real, False, ts),
                                              self._nonres(runs, real, 2, 0x8000)])
        self.paths[path] = n
        self.sizes[path] = (n, runs)
        return n

    def delete(self, path: str) -> None:
        n, runs = self.sizes.pop(path)
        rec = bytearray(self.records[n])
        flags = struct.unpack_from("<H", rec, 22)[0] & ~1
        struct.pack_into("<H", rec, 22, flags)
        self.records[n] = bytes(rec)
        for lcn, ln in runs:
            if lcn is not None:
                for c in range(lcn, lcn + ln):
                    self._set(c, False)
        del self.paths[path]

    def build(self) -> bytes:
        # $Bitmap record 6 (written last so its content reflects every allocation)
        bm_len = len(self.bitmap)
        self.records[6] = self._record(6, 1, [self._filename(5, "$Bitmap", bm_len, False, 0),
                                              self._nonres([(self.bitmap_lcn, 1)], bm_len, 2)])
        for n, rec in self.records.items():
            off = self.mft_lcn * self.cs + n * self.REC
            self.img[off:off + self.REC] = rec
        self.img[self.bitmap_lcn * self.cs:self.bitmap_lcn * self.cs + bm_len] = self.bitmap
        boot = bytearray(SECTOR)
        boot[0:3] = b"\xeb\x52\x90"
        boot[3:11] = b"NTFS    "
        struct.pack_into("<HB", boot, 11, self.BPS, self.SPC)
        boot[21] = 0xF8
        struct.pack_into("<HH", boot, 24, 63, 255)
        struct.pack_into("<Q", boot, 40, self.total - 1)
        struct.pack_into("<QQ", boot, 48, self.mft_lcn, 2)
        boot[64] = 0xF6  # 2^10 = 1024-byte MFT records
        boot[68] = 1
        struct.pack_into("<Q", boot, 72, 0x1122334455667788)
        boot[510:512] = b"\x55\xaa"
        self.img[0:SECTOR] = boot
        self.img[(self.total - 1) * SECTOR:] = boot  # backup boot sector in the last sector
        mirror = 2 * self.cs  # $MFTMirr: copy of the first four MFT records
        self.img[mirror:mirror + 4 * self.REC] = self.img[self.mft_lcn * self.cs:self.mft_lcn * self.cs + 4 * self.REC]
        return bytes(self.img)


# ====================================================================== ext2 / ext4
class ExtBuilder:
    BS = 1024

    def __init__(self, blocks: int = 8192, extents: bool = True):
        self.blocks, self.extents = blocks, extents
        self.ipg, self.isize = 128, 256
        self.img = bytearray(blocks * self.BS)
        self.itable = 5
        self.data_start = self.itable + self.ipg * self.isize // self.BS
        self.bbitmap = bytearray(self.BS)
        self.ibitmap = bytearray(self.BS)
        for b in range(0, self.data_start):  # everything before the data area is in use
            self._bset(b, True)
        for i in range(1, 12):
            self._iset(i, True)
        self.inodes: dict[int, bytearray] = {}
        self.dirs: dict[str, list[tuple[int, str, int]]] = {"": []}  # path -> [(inode, name, ftype)]
        self.dir_inode: dict[str, int] = {"": 2}
        self.next_ino = 12
        self.files: dict[str, tuple[int, str, int, list[int]]] = {}  # path -> (inode, dir, entry index, blocks)
        self.hidden: dict[str, list[tuple[int, str, int]]] = {}  # deleted entries kept as slack
        self.dirs_blocks: dict[str, int] = {}

    def _bset(self, b: int, on: bool) -> None:
        k = b - 1
        if on:
            self.bbitmap[k // 8] |= 1 << (k % 8)
        else:
            self.bbitmap[k // 8] &= ~(1 << (k % 8)) & 0xFF

    def _bfree(self, b: int) -> bool:
        return not self.bbitmap[(b - 1) // 8] >> ((b - 1) % 8) & 1

    def _iset(self, i: int, on: bool) -> None:
        k = i - 1
        if on:
            self.ibitmap[k // 8] |= 1 << (k % 8)
        else:
            self.ibitmap[k // 8] &= ~(1 << (k % 8)) & 0xFF

    def _alloc(self, n: int) -> list[int]:
        run, start = 0, self.data_start
        for b in range(self.data_start, self.blocks):
            if self._bfree(b):
                if run == 0:
                    start = b
                run += 1
                if run == n:
                    blocks = list(range(start, start + n))
                    for x in blocks:
                        self._bset(x, True)
                    return blocks
            else:
                run = 0
        raise RuntimeError("image full")

    def _inode(self, mode: int, size: int, blocks: list[int], ts: int, dtime: int = 0, links: int = 1) -> bytearray:
        ino = bytearray(self.isize)
        struct.pack_into("<HHIIIII", ino, 0, mode, 0, size & 0xFFFFFFFF, ts, ts, ts, dtime)
        struct.pack_into("<HHI", ino, 24, 0, links, len(blocks) * 2)
        flags = 0x80000 if self.extents else 0
        struct.pack_into("<I", ino, 32, flags)
        struct.pack_into("<I", ino, 108, size >> 32)
        ib = bytearray(60)
        if self.extents:
            struct.pack_into("<HHHHI", ib, 0, 0xF30A, 1 if blocks else 0, 4, 0, 0)
            if blocks:
                # contiguous allocation => one extent
                struct.pack_into("<IHHI", ib, 12, 0, len(blocks), 0, blocks[0])
        else:
            for i, b in enumerate(blocks[:12]):
                struct.pack_into("<I", ib, 4 * i, b)
            if len(blocks) > 12:  # single indirect block
                ind = self._alloc(1)[0]
                struct.pack_into("<I", ib, 48, ind)
                struct.pack_into(f"<{len(blocks) - 12}I", self.img, ind * self.BS, *blocks[12:])
        ino[40:100] = ib
        return ino

    def _write_dir(self, path: str) -> None:
        entries = self.dirs[path]
        parent_ino = self.dir_inode[path.rpartition("/")[0]] if path else 2
        raw = bytearray()
        items = [(self.dir_inode[path], ".", 2), (parent_ino, "..", 2)] + entries
        hidden = self.hidden.get(path, [])
        for i, (ino, name, ft) in enumerate(items):
            nb = name.encode()
            actual = 8 + ((len(nb) + 3) & ~3)
            last = i == len(items) - 1
            slack = b""
            if hidden and last:
                for h_ino, h_name, h_ft in hidden:
                    hb = h_name.encode()
                    h_len = 8 + ((len(hb) + 3) & ~3)
                    slack += struct.pack("<IHBB", h_ino, h_len, len(hb), h_ft) + hb.ljust(h_len - 8, b"\0")
            rec_len = actual + len(slack)
            raw += struct.pack("<IHBB", ino, rec_len, len(nb), ft) + nb.ljust(actual - 8, b"\0") + slack
        # stretch the last entry to the end of the block
        last_off = len(raw) - (actual + len(slack))
        struct.pack_into("<H", raw, last_off + 4, self.BS - last_off)
        raw = raw.ljust(self.BS, b"\0")
        blk = self.dirs_blocks[path]
        self.img[blk * self.BS:(blk + 1) * self.BS] = raw

    def _ensure_dir(self, path: str) -> None:
        if path in self.dirs_blocks:
            return
        parent = path.rpartition("/")[0]
        if path:
            self._ensure_dir(parent)
            ino = self.next_ino
            self.next_ino += 1
            self.dir_inode[path] = ino
            self.dirs[path] = []
            self._iset(ino, True)
            self.dirs[parent].append((ino, path.rpartition("/")[2], 2))
        blk = self._alloc(1)[0]
        self.dirs_blocks[path] = blk
        ino = self.dir_inode[path]
        self.inodes[ino] = self._inode(0x41ED, self.BS, [blk], 1_700_000_000, links=2)

    def add_file(self, path: str, data: bytes, ts: int = 1_700_000_000) -> int:
        parent, _, name = path.rpartition("/")
        self._ensure_dir("")
        self._ensure_dir(parent)
        n = (len(data) + self.BS - 1) // self.BS
        blocks = self._alloc(n) if n else []
        padded = data.ljust(n * self.BS, b"\0")
        for i, b in enumerate(blocks):
            self.img[b * self.BS:(b + 1) * self.BS] = padded[i * self.BS:(i + 1) * self.BS]
        ino = self.next_ino
        self.next_ino += 1
        self._iset(ino, True)
        self.inodes[ino] = self._inode(0x81A4, len(data), blocks, ts)
        self.dirs[parent].append((ino, name, 1))
        self.files[path] = (ino, parent, len(self.dirs[parent]) - 1, blocks)
        return ino

    def delete(self, path: str, now: int = 1_700_100_000) -> None:
        ino, parent, _idx, blocks = self.files.pop(path)
        dead = bytearray(self.inodes[ino])
        struct.pack_into("<I", dead, 20, now)
        struct.pack_into("<H", dead, 26, 0)
        if self.extents:  # ext4 zeroes the entry count but leaves the extent bytes (ext2 keeps its block pointers)
            struct.pack_into("<H", dead, 42, 0)
        self.inodes[ino] = dead
        self._iset(ino, False)
        for b in blocks:
            self._bset(b, False)
        entry = next(e for e in self.dirs[parent] if e[0] == ino)
        self.dirs[parent].remove(entry)
        self.hidden.setdefault(parent, []).append(entry)

    def build(self) -> bytes:
        self._ensure_dir("")
        for path in list(self.dirs_blocks):
            self._write_dir(path)
        sb = bytearray(1024)
        struct.pack_into("<IIIIII", sb, 0, self.ipg, self.blocks, 0, 0, 0, 1)
        struct.pack_into("<I", sb, 24, 0)
        struct.pack_into("<III", sb, 32, self.blocks, self.blocks, self.ipg)
        struct.pack_into("<HHHH", sb, 54, 0, 0xEF53, 1, 1)
        struct.pack_into("<I", sb, 76, 1)
        struct.pack_into("<IH", sb, 84, 11, self.isize)
        struct.pack_into("<I", sb, 96, 0x2 | (0x40 if self.extents else 0))
        self.img[1024:2048] = sb
        gd = bytearray(32)
        struct.pack_into("<III", gd, 0, 3, 4, self.itable)
        self.img[2 * self.BS:2 * self.BS + 32] = gd
        self.img[3 * self.BS:4 * self.BS] = self.bbitmap
        self.img[4 * self.BS:5 * self.BS] = self.ibitmap
        for ino, raw in self.inodes.items():
            off = self.itable * self.BS + (ino - 1) * self.isize
            self.img[off:off + self.isize] = raw
        return bytes(self.img)


# ====================================================================== whole disks
def make_mbr_disk(volumes: list[tuple[bytes, int]], total_mib: int | None = None, align: int = 1 << 20,
                  boot_code: bytes = b"") -> tuple[bytes, list[tuple[int, int]]]:
    """volumes: [(image, mbr type)]. Returns (disk bytes, [(start, size)])."""
    pos = align
    layout = []
    for img, _t in volumes:
        layout.append((pos, len(img)))
        pos += len(img) + (-len(img) % align)
    size = max(pos + align, (total_mib or 0) << 20)
    disk = bytearray(size)
    mbr = bytearray(512)
    mbr[:len(boot_code)] = boot_code[:446]
    for i, ((img, t), (start, ln)) in enumerate(zip(volumes, layout)):
        disk[start:start + ln] = img
        mbr[446 + 16 * i:462 + 16 * i] = struct.pack("<B3sB3sII", 0x80 if i == 0 else 0, b"\xfe\xff\xff", t, b"\xfe\xff\xff",
                                                     start // 512, ln // 512)
    mbr[510:512] = b"\x55\xaa"
    disk[:512] = mbr
    return bytes(disk), layout
