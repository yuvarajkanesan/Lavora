"""Virtual RAID: rebuild a RAID 0/1/5/6/10 array from member images or disks, read-only, with automatic parameter detection.

RaidSource is an ordinary BlockSource, so every other feature (partition scan, filesystem parsing, carving, imaging) works
on a rebuilt array unchanged. Missing members (None) are reconstructed from parity where the level allows: RAID 5 survives
one missing member, RAID 6 two (GF(2^8) with the md polynomial 0x11d), RAID 1/10 need one copy per mirror.

Layouts (md naming): "ls" left-symmetric (Linux md default), "la" left-asymmetric, "rs" right-symmetric, "ra" right-asymmetric.
RAID 6 uses left-symmetric with Q after P. RAID 10 is "near 2" (adjacent members mirror each other).
A spec string lets CLI/GUI pass an array around:  raid:level=5;chunk=65536;layout=ls;members=a.img|b.img|?|d.img
"""
from __future__ import annotations

import hashlib
import itertools
from dataclasses import dataclass

from .device import SECTOR, BlockSource, PartitionView
from .fs import FsError

LAYOUTS = ("ls", "la", "rs", "ra")
CHUNK_SIZES = (4096, 8192, 16384, 32768, 65536, 131072, 262144, 524288, 1048576)

# ---------------------------------------------------------------- GF(256)
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def gf_mul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def gf_div(a: int, b: int) -> int:
    if b == 0:
        raise ZeroDivisionError
    return 0 if a == 0 else _EXP[_LOG[a] - _LOG[b] + 255]


def gf_pow2(n: int) -> int:
    return _EXP[n % 255]


def _xor(a: bytes, b: bytes) -> bytes:
    return (int.from_bytes(a, "little") ^ int.from_bytes(b, "little")).to_bytes(len(a), "little")


def _scale(data: bytes, coeff: int) -> bytes:
    if coeff == 1:
        return data
    table = bytes(gf_mul(i, coeff) for i in range(256))
    return data.translate(table)


# ---------------------------------------------------------------- layout maths (one place, used by reads and by detection)
def raid5_row(row: int, d: int, n: int, layout: str) -> tuple[int, int]:
    """(parity member, data member) for data chunk index d (0..n-2) in stripe row `row`."""
    if layout in ("ls", "la"):
        pd = (n - 1) - (row % n)
    else:
        pd = row % n
    disk = (pd + 1 + d) % n if layout in ("ls", "rs") else (d if d < pd else d + 1)
    return pd, disk


def raid6_row(row: int, d: int, n: int) -> tuple[int, int, int]:
    pd = (n - 1) - (row % n)
    return pd, (pd + 1) % n, (pd + 2 + d) % n


class RaidError(Exception):
    pass


class RaidSource(BlockSource):
    def __init__(self, members: list[BlockSource | None], level: int, chunk: int = 65536, layout: str = "ls"):
        n = len(members)
        if level not in (0, 1, 5, 6, 10):
            raise RaidError(f"unsupported RAID level {level}")
        if level == 5 and n < 3 or level == 6 and n < 4 or level == 10 and (n < 4 or n % 2) or level in (0, 1) and n < 2:
            raise RaidError(f"RAID {level} cannot be built from {n} members")
        if layout not in LAYOUTS:
            raise RaidError(f"unknown layout {layout}")
        present = [m for m in members if m is not None]
        missing = n - len(present)
        limit = {0: 0, 1: n - 1, 5: 1, 6: 2, 10: n // 2}[level]
        if missing > limit or not present:
            raise RaidError(f"RAID {level} with {missing} of {n} members missing cannot be reconstructed")
        if level == 0 and missing:
            raise RaidError("RAID 0 has no redundancy")
        if level == 10 and any(members[i] is None and members[i + 1] is None for i in range(0, n, 2)):
            raise RaidError("RAID 10: both members of a mirror pair are missing, that data is gone")
        self.members, self.level, self.chunk, self.layout, self.n = members, level, chunk, layout, n
        ms = min(m.size for m in present)
        self.member_size = ms - ms % chunk if level != 1 else ms
        data_disks = {0: n, 1: 1, 5: n - 1, 6: n - 2, 10: n // 2}[level]
        self.size = self.member_size * data_disks
        self.path = "raid:" + "|".join(m.path if m else "?" for m in members)
        self.is_device = any(m.is_device for m in present)

    # ---- member access
    def _mread(self, i: int, off: int, ln: int) -> bytes:
        m = self.members[i]
        if m is None:
            raise OSError("member missing")
        data = m.read_at(off, ln)
        return data.ljust(ln, b"\0")

    def _alive(self, i: int) -> bool:
        return self.members[i] is not None

    def _chunk_data(self, chunk_no: int, inner: int, ln: int) -> bytes:
        n, c = self.n, self.chunk
        if self.level == 0:
            return self._mread(chunk_no % n, (chunk_no // n) * c + inner, ln)
        if self.level == 1:
            return self._mread(next(i for i in range(n) if self._alive(i)), chunk_no * c + inner, ln)
        if self.level == 10:
            pairs = n // 2
            pair, row = chunk_no % pairs, chunk_no // pairs
            for i in (2 * pair, 2 * pair + 1):
                if self._alive(i):
                    return self._mread(i, row * c + inner, ln)
            raise OSError("both members of a mirror are missing")
        if self.level == 5:
            row, d = divmod(chunk_no, n - 1)
            _pd, disk = raid5_row(row, d, n, self.layout)
            off = row * c + inner
            if self._alive(disk):
                return self._mread(disk, off, ln)
            out = bytes(ln)
            for i in range(n):
                if i != disk:
                    out = _xor(out, self._mread(i, off, ln))
            return out
        return self._raid6_chunk(chunk_no, inner, ln)

    def _raid6_chunk(self, chunk_no: int, inner: int, ln: int) -> bytes:
        n, c = self.n, self.chunk
        row, d = divmod(chunk_no, n - 2)
        pd, qd, _ = raid6_row(row, 0, n)
        off = row * c + inner
        disks = [raid6_row(row, k, n)[2] for k in range(n - 2)]
        if self._alive(disks[d]):
            return self._mread(disks[d], off, ln)
        missing = [k for k in range(n - 2) if not self._alive(disks[k])]
        known = {k: self._mread(disks[k], off, ln) for k in range(n - 2) if k not in missing}
        p_ok, q_ok = self._alive(pd), self._alive(qd)
        p_prime = q_prime = None
        if p_ok:
            p_prime = self._mread(pd, off, ln)
            for v in known.values():
                p_prime = _xor(p_prime, v)
        if q_ok:
            q_prime = self._mread(qd, off, ln)
            for k, v in known.items():
                q_prime = _xor(q_prime, _scale(v, gf_pow2(k)))
        if len(missing) == 1:
            if p_prime is not None:
                return p_prime
            if q_prime is not None:
                return _scale(q_prime, gf_div(1, gf_pow2(d)))
        elif len(missing) == 2 and p_prime is not None and q_prime is not None:
            a, b = missing
            ga, gb = gf_pow2(a), gf_pow2(b)
            da = _scale(_xor(q_prime, _scale(p_prime, gb)), gf_div(1, ga ^ gb))
            return da if d == a else _xor(p_prime, da)
        raise OSError("not enough surviving members to rebuild this stripe")

    def read_at(self, offset: int, length: int) -> bytes:
        if offset < 0 or offset >= self.size or length <= 0:
            return b""
        length = min(length, self.size - offset)
        out = bytearray()
        pos = offset
        while len(out) < length:
            chunk_no, inner = divmod(pos, self.chunk)
            take = min(self.chunk - inner, length - len(out))
            out += self._chunk_data(chunk_no, inner, take)
            pos += take
        return bytes(out)

    def close(self) -> None:
        for m in self.members:
            if m is not None:
                m.close()


# ---------------------------------------------------------------- spec strings
def _parse_size(v: str) -> int:
    v = v.strip().lower()
    mult = {"k": 1024, "m": 1024 ** 2}.get(v[-1:], 1)
    return int(v[:-1] if mult != 1 else v) * mult


def spec_for(level: int, chunk: int, layout: str, member_paths: list[str | None]) -> str:
    return f"raid:level={level};chunk={chunk};layout={layout};members=" + "|".join(p or "?" for p in member_paths)


def open_raid_spec(spec: str) -> RaidSource:
    from .device import open_source
    body = spec[len("raid:"):]
    kv = dict(part.split("=", 1) for part in body.split(";") if "=" in part)
    if "members" not in kv or "level" not in kv:
        raise RaidError("a RAID spec needs level= and members=")
    members = [None if p == "?" else open_source(p) for p in kv["members"].split("|")]
    return RaidSource(members, int(kv["level"]), _parse_size(kv.get("chunk", "64k")), kv.get("layout", "ls"))


# ---------------------------------------------------------------- detection
@dataclass
class Candidate:
    level: int
    chunk: int
    layout: str
    order: tuple[int, ...]  # order[i] = index (into the list you passed) of the member that sits in slot i
    score: int

    def describe(self) -> str:
        return f"RAID {self.level}, chunk {self.chunk // 1024} KiB, layout {self.layout}, member order {list(self.order)}, score {self.score}"


def _sample_positions(members: list[BlockSource], count: int = 64, span: int = 4096) -> list[int]:
    size = min(m.size for m in members)
    step = max(span, (size // count) // span * span)
    return list(range(0, size - span, step))[:count]


def looks_like_raid5(members: list[BlockSource]) -> float:
    """Share of non-empty sample positions where the XOR of all members is zero (true for any RAID 5 layout)."""
    good = total = 0
    for pos in _sample_positions(members):
        blocks = [m.read_at(pos, 4096).ljust(4096, b"\0") for m in members]
        if not any(any(b) for b in blocks):
            continue
        acc = bytes(4096)
        for b in blocks:
            acc = _xor(acc, b)
        total += 1
        good += not any(acc)
    return good / total if total else 0.0


def looks_like_mirror(a: BlockSource, b: BlockSource) -> bool:
    return all(a.read_at(pos, 4096) == b.read_at(pos, 4096) for pos in _sample_positions([a, b], 32))


_HEAD = {"jpg": b"\xff\xd8\xff", "jpeg": b"\xff\xd8\xff", "png": b"\x89PNG\r\n\x1a\n", "gif": b"GIF8", "pdf": b"%PDF-",
         "zip": b"PK\x03\x04", "docx": b"PK\x03\x04", "xlsx": b"PK\x03\x04", "pptx": b"PK\x03\x04", "7z": b"7z\xbc\xaf",
         "sqlite": b"SQLite format 3", "db": b"SQLite format 3", "mp4": None, "exe": b"MZ", "dll": b"MZ"}
_TAIL = {"jpg": b"\xff\xd9", "jpeg": b"\xff\xd9", "png": b"IEND", "pdf": b"%%EOF"}
_TEXT = {"txt", "log", "csv", "json", "xml", "py", "md", "ini", "html", "htm", "js", "css", "yml", "yaml"}


def _file_checks(fs, limit: int = 40) -> int:
    """Do files look like what their names say, at BOTH ends? A file's first and last bytes sit in different chunks, so a wrong
    layout / chunk size / order breaks them. +3 per good check, -3 per bad one, over the first `limit` suitable files."""
    score = seen = 0
    for e in fs.scan():
        if e.is_dir or e.size < 16 or e.overwritten_pct > 0 or e.note.startswith("unsupported"):
            continue
        ext = e.extension
        if ext not in _HEAD and ext not in _TEXT:
            continue
        try:
            head = fs.read(e, 0, 64)
            if ext in _TEXT:
                ok = sum(32 <= b < 127 or b in (9, 10, 13) for b in head) >= len(head) * 0.95
                score += 3 if ok else -3
            elif _HEAD[ext] is not None:
                score += 3 if head.startswith(_HEAD[ext]) else -3
            tail = _TAIL.get(ext)
            if tail:
                score += 3 if tail in fs.read(e, max(0, e.size - 16), 16) else -3
        except (OSError, FsError, ValueError):
            score -= 3
        seen += 1
        if seen >= limit:
            break
    return score


def _deep_check(arr: BlockSource, start: int, size: int, kind: str) -> int:
    """Structures that live far from the start of a volume and must agree with ones near it. A wrong chunk size / layout /
    member order scrambles the distance between them, so agreement is strong evidence the geometry is right."""
    import struct
    boot = arr.read_at(start, SECTOR)
    if kind == "ntfs":  # backup boot sector in the volume's last sector
        return 200 if arr.read_at(start + size - SECTOR, SECTOR) == boot else 0
    if kind == "fat32":  # second FAT copy must equal the first
        reserved, = struct.unpack_from("<H", boot, 14)
        fat_sectors, = struct.unpack_from("<I", boot, 36)
        one = arr.read_at(start + reserved * SECTOR, 4096)
        two = arr.read_at(start + (reserved + fat_sectors) * SECTOR, 4096)
        return 150 if any(one) and one == two else 0
    return 0


def score_array(arr: BlockSource) -> int:
    """How much of a sane partition table / filesystem the virtual array shows. Higher is better; 0 means nothing sane."""
    from .partitions import identify_fs, parse_gpt, read_partitions
    score = 0
    from .fs import open_filesystem
    try:
        _scheme, parts = read_partitions(arr)
        vols = [(p.start, p.size) for p in parts if p.size]
        if vols:
            score += 100
        elif identify_fs(arr, 0):
            vols = [(0, arr.size)]
        info = parse_gpt(arr) if arr.read_at(SECTOR, 8) == b"EFI PART" else None
        if info and info.primary_ok and info.backup_ok:
            score += 300  # the backup GPT sits in the last sector of the array: only a correct size/layout reproduces it
        for start, size in vols[:4]:
            kind = identify_fs(arr, start)
            if not kind:
                continue
            score += 100 + _deep_check(arr, start, size, kind)
            try:
                fs = open_filesystem(PartitionView(arr, start, size))
                score += _file_checks(fs)
            except (FsError, OSError, ValueError, IndexError, KeyError):
                score -= 50
    except (OSError, ValueError, IndexError, KeyError, struct_error):
        return 0
    return score


try:  # struct.error is a distinct exception type
    from struct import error as struct_error
except ImportError:  # pragma: no cover
    struct_error = ValueError  # type: ignore[assignment,misc]


def duplicate_chunks(arr: BlockSource, chunk: int, limit: int = 512) -> int:
    """Count non-empty chunks that appear twice among the first `limit` chunks of the array.

    On a zero-heavy disk the parity chunk of a stripe equals its only non-zero data chunk, so a WRONG layout leaks parity into
    the data stream as exact duplicates; in a correct reconstruction non-empty chunks are essentially unique."""
    seen: set[bytes] = set()
    dups = 0
    for i in range(min(limit, arr.size // chunk)):
        data = arr.read_at(i * chunk, chunk)
        if not any(data):
            continue
        key = hashlib.blake2b(data, digest_size=16).digest()
        if key in seen:
            dups += 1
        seen.add(key)
    return dups


def _plausible_start(arr: BlockSource) -> bool:
    head = arr.read_at(0, SECTOR)
    return bool(head) and (head[510:512] == b"\x55\xaa" or head[3:11] in (b"NTFS    ", b"EXFAT   ")
                          or arr.read_at(SECTOR, 8) == b"EFI PART" or arr.read_at(1080, 2) == b"\x53\xef")


def detect(members: list[BlockSource], *, levels: tuple[int, ...] | None = None, chunk_sizes: tuple[int, ...] = CHUNK_SIZES,
           max_members_permuted: int = 6, top: int = 5) -> list[Candidate]:
    """Find level, chunk size, layout and member order. Returns candidates best-first (empty if nothing looks like a volume)."""
    n = len(members)
    if n < 2:
        raise RaidError("need at least two members")
    if levels is None:
        lv: list[int] = []
        if n == 2 and looks_like_mirror(members[0], members[1]):
            lv = [1]
        else:
            if n >= 3 and looks_like_raid5(members) >= 0.9:
                lv.append(5)
            if n >= 4 and n % 2 == 0 and all(looks_like_mirror(members[i], members[i + 1]) for i in range(0, n, 2)):
                lv.append(10)
            if not lv:
                lv = [0] + ([6] if n >= 4 else [])
        levels = tuple(lv)
    if n > max_members_permuted:
        orders = [tuple(range(n))]
    else:
        orders = list(itertools.permutations(range(n)))
    found: list[Candidate] = []
    for level in levels:
        if level == 1:
            found.append(Candidate(1, SECTOR, "ls", tuple(range(n)), score_array(RaidSource(list(members), 1))))
            continue
        layouts = LAYOUTS if level == 5 else ("ls",)
        for chunk in chunk_sizes:
            for layout in layouts:
                for order in orders:
                    if level == 10 and any(order[i] > order[i + 1] for i in range(0, n, 2)):
                        continue  # swapping the two halves of a mirror changes nothing
                    try:
                        arr = RaidSource([members[i] for i in order], level, chunk, layout)
                    except RaidError:
                        continue
                    if not _plausible_start(arr):
                        continue  # cheap filter: sector 0 must look like a boot sector / partition table
                    s = score_array(arr)
                    if s > 0:
                        found.append(Candidate(level, chunk, layout, order, s))
    found.sort(key=lambda c: -c.score)
    # tie-break the leaders: geometry that leaks parity into the data stream shows up as duplicated chunks
    leaders = [c for c in found if c.score >= (found[0].score - 60)][:60] if found else []
    for c in leaders:
        if c.level in (5, 6):
            arr = RaidSource([members[i] for i in c.order], c.level, c.chunk, c.layout)
            c.score -= 40 * duplicate_chunks(arr, c.chunk)
    found.sort(key=lambda c: -c.score)
    return found[:top]
