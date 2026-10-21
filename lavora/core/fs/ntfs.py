"""Read-only NTFS parser: walks the $MFT, lists live and deleted files, reads resident and non-resident data.

Scope: unnamed $DATA streams, resident + non-resident (data runs, sparse), $Bitmap-based overwrite estimate.
Not handled (reported as `unsupported`): compressed/encrypted streams and files whose $DATA lives in extension
records behind an $ATTRIBUTE_LIST.
"""
from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass, field

from ..device import BlockSource
from .base import FileEntry, Filesystem, FsError, UnsupportedError, filetime_to_dt

ATTR_STDINFO, ATTR_ATTRLIST, ATTR_FILENAME, ATTR_DATA, ATTR_END = 0x10, 0x20, 0x30, 0x80, 0xFFFFFFFF
ROOT_RECORD = 5
BITMAP_RECORD = 6


@dataclass
class DataStream:
    resident: bytes | None = None
    runs: list[tuple[int | None, int]] = field(default_factory=list)  # (lcn or None for sparse, cluster count)
    real_size: int = 0
    init_size: int = 0
    flags: int = 0


@dataclass
class MftRecord:
    number: int
    in_use: bool
    is_dir: bool
    base_ref: int
    parent: int = 0
    name: str = ""
    mtime: int = 0
    ctime: int = 0
    data: DataStream | None = None
    has_attr_list: bool = False


def apply_fixups(rec: bytearray, sector: int = 512) -> bool:
    """Undo the update-sequence protection; False if the record is torn/corrupt."""
    off, count = struct.unpack_from("<HH", rec, 4)
    if off + 2 * count > len(rec) or count < 1:
        return False
    usn = rec[off:off + 2]
    for i in range(1, count):
        end = i * sector - 2
        if end + 2 > len(rec) or rec[end:end + 2] != usn:
            return False
        rec[end:end + 2] = rec[off + 2 * i:off + 2 * i + 2]
    return True


def parse_runs(raw: bytes) -> list[tuple[int | None, int]]:
    runs: list[tuple[int | None, int]] = []
    pos, lcn = 0, 0
    while pos < len(raw) and raw[pos]:
        h = raw[pos]
        ln, on = h & 15, h >> 4
        pos += 1
        if pos + ln + on > len(raw) or ln == 0:
            break
        length = int.from_bytes(raw[pos:pos + ln], "little")
        pos += ln
        if on == 0:
            runs.append((None, length))
        else:
            lcn += int.from_bytes(raw[pos:pos + on], "little", signed=True)
            runs.append((lcn, length))
        pos += on
    return runs


def parse_record(number: int, rec: bytearray) -> MftRecord | None:
    if rec[:4] != b"FILE" or not apply_fixups(rec):
        return None
    flags = struct.unpack_from("<H", rec, 22)[0]
    base = struct.unpack_from("<Q", rec, 32)[0] & 0xFFFFFFFFFFFF
    out = MftRecord(number, bool(flags & 1), bool(flags & 2), base)
    pos = struct.unpack_from("<H", rec, 20)[0]
    best_ns = -1
    prio = {1: 3, 3: 4, 0: 2, 2: 1}  # Win32&DOS > Win32 > POSIX > DOS
    while pos + 16 <= len(rec):
        atype, alen = struct.unpack_from("<II", rec, pos)
        if atype == ATTR_END or alen < 16 or pos + alen > len(rec):
            break
        nonres, nlen = rec[pos + 8], rec[pos + 9]
        aflags = struct.unpack_from("<H", rec, pos + 12)[0]
        if atype == ATTR_STDINFO and not nonres:
            coff = struct.unpack_from("<H", rec, pos + 20)[0]
            out.ctime, out.mtime = struct.unpack_from("<QQ", rec, pos + coff)
        elif atype == ATTR_FILENAME and not nonres:
            coff = struct.unpack_from("<H", rec, pos + 20)[0]
            b = pos + coff
            ns = rec[b + 65]
            if prio.get(ns, 0) > best_ns:
                best_ns = prio.get(ns, 0)
                out.parent = struct.unpack_from("<Q", rec, b)[0] & 0xFFFFFFFFFFFF
                out.name = bytes(rec[b + 66:b + 66 + 2 * rec[b + 64]]).decode("utf-16-le", "replace")
        elif atype == ATTR_ATTRLIST:
            out.has_attr_list = True
        elif atype == ATTR_DATA and nlen == 0 and out.data is None:
            ds = DataStream(flags=aflags)
            if not nonres:
                size, coff = struct.unpack_from("<IH", rec, pos + 16)
                ds.resident = bytes(rec[pos + coff:pos + coff + size])
                ds.real_size = ds.init_size = size
            else:
                runoff = struct.unpack_from("<H", rec, pos + 32)[0]
                ds.real_size, ds.init_size = struct.unpack_from("<QQ", rec, pos + 48)
                ds.runs = parse_runs(bytes(rec[pos + runoff:pos + alen]))
            out.data = ds
        pos += alen
    return out


class NtfsFilesystem(Filesystem):
    name = "ntfs"

    def __init__(self, source: BlockSource):
        super().__init__(source)
        boot = source.read_at(0, 512)
        if len(boot) < 512 or boot[3:11] != b"NTFS    ":
            raise FsError("not an NTFS volume (boot sector signature missing)")
        self.bps, self.spc = struct.unpack_from("<HB", boot, 11)
        self.total_sectors = struct.unpack_from("<Q", boot, 40)[0]
        mft_lcn = struct.unpack_from("<Q", boot, 48)[0]
        cpr = struct.unpack_from("<b", boot, 64)[0]
        self.cluster = self.bps * self.spc
        self.record_size = cpr * self.cluster if cpr > 0 else 1 << -cpr
        if not (self.bps and self.spc and 256 <= self.record_size <= 65536):
            raise FsError("implausible NTFS geometry")
        self.total_clusters = self.total_sectors // self.spc
        first = bytearray(source.read_at(mft_lcn * self.cluster, self.record_size))
        rec0 = parse_record(0, first)
        if not rec0 or not rec0.data:
            raise FsError("$MFT record 0 is unreadable")
        self.mft = rec0.data
        self.mft_count = self.mft.real_size // self.record_size
        self._bitmap: bytes | None = None

    # ---------------------------------------------------------------- low-level reads
    def _read_runs(self, ds: DataStream, offset: int, length: int, size_limit: int) -> bytes:
        if offset >= size_limit:
            return b""
        length = min(length, size_limit - offset)
        out = bytearray()
        vpos = 0  # byte position of the current run in the stream
        for lcn, count in ds.runs:
            rlen = count * self.cluster
            if vpos + rlen <= offset:
                vpos += rlen
                continue
            if vpos >= offset + length:
                break
            start = max(offset, vpos) - vpos
            take = min(rlen - start, offset + length - max(offset, vpos))
            out += bytes(take) if lcn is None else self.source.read_at(lcn * self.cluster + start, take)
            vpos += rlen
        # shorter than requested means the runs ended early (damaged); pad with zeros up to the real size
        out += bytes(length - len(out)) if len(out) < length else b""
        return bytes(out)

    def _read_stream(self, ds: DataStream, offset: int, length: int) -> bytes:
        if ds.resident is not None:
            return ds.resident[offset:offset + length]
        data = self._read_runs(ds, offset, length, ds.real_size)
        if ds.init_size < ds.real_size:  # bytes past the initialised size read as zeros
            cut = max(0, ds.init_size - offset)
            if cut < len(data):
                data = data[:cut] + bytes(len(data) - cut)
        return data

    def _record(self, number: int) -> MftRecord | None:
        raw = self._read_runs(self.mft, number * self.record_size, self.record_size, self.mft.real_size)
        return parse_record(number, bytearray(raw)) if len(raw) == self.record_size else None

    # ---------------------------------------------------------------- MFT walk
    def _iter_records(self, progress=None) -> Iterator[MftRecord]:
        batch = max(1, (1 << 20) // self.record_size)
        n = 0
        while n < self.mft_count:
            cnt = min(batch, self.mft_count - n)
            raw = self._read_runs(self.mft, n * self.record_size, cnt * self.record_size, self.mft.real_size)
            for i in range(cnt):
                chunk = raw[i * self.record_size:(i + 1) * self.record_size]
                if len(chunk) < self.record_size:
                    break
                rec = parse_record(n + i, bytearray(chunk))
                if rec and rec.base_ref == 0 and rec.name:
                    yield rec
            n += cnt
            if progress:
                progress(n, self.mft_count)

    def _bitmap_bits(self) -> bytes:
        if self._bitmap is None:
            rec = self._record(BITMAP_RECORD)
            self._bitmap = self._read_stream(rec.data, 0, rec.data.real_size) if rec and rec.data else b""
        return self._bitmap

    def _allocated_pct(self, ds: DataStream) -> float:
        bm = self._bitmap_bits()
        if not bm or not ds.runs:
            return 0.0
        total = used = 0
        for lcn, count in ds.runs:
            if lcn is None:
                continue
            for c in range(lcn, lcn + count):
                total += 1
                if c // 8 < len(bm) and bm[c // 8] >> (c % 8) & 1:
                    used += 1
        return 100.0 * used / total if total else 0.0

    def scan(self, *, include_live: bool = True, progress=None) -> Iterator[FileEntry]:
        recs = {r.number: r for r in self._iter_records(progress)}
        paths: dict[int, str] = {}

        def path_of(n: int, depth: int = 0) -> str:
            if n in paths:
                return paths[n]
            r = recs.get(n)
            if n == ROOT_RECORD:
                p = ""
            elif r is None or depth > 64:
                p = "$OrphanFiles"
            else:
                parent = path_of(r.parent, depth + 1) if r.parent != n else "$OrphanFiles"
                p = f"{parent}/{r.name}" if parent else r.name
            paths[n] = p
            return p

        for n, r in sorted(recs.items()):
            if n < 16 and n != ROOT_RECORD:  # NTFS metadata files ($MFT, $Bitmap, ...)
                continue
            if n == ROOT_RECORD or (r.in_use and not include_live):
                continue
            entry = FileEntry(path=path_of(n), name=r.name, size=r.data.real_size if r.data else 0, is_dir=r.is_dir,
                              deleted=not r.in_use, mtime=filetime_to_dt(r.mtime), ctime=filetime_to_dt(r.ctime),
                              ref=n, fs="ntfs")
            if not r.is_dir:
                if r.data is None:
                    entry.note = "unsupported: no unnamed data stream in the base record" if r.has_attr_list else ""
                    entry.size = 0
                elif r.data.flags & 0x0001:
                    entry.note = "unsupported: compressed"
                elif r.data.flags & 0x4000:
                    entry.note = "unsupported: encrypted (EFS)"
                elif not r.in_use:
                    entry.overwritten_pct = self._allocated_pct(r.data)
            yield entry

    def read(self, entry: FileEntry, offset: int = 0, length: int | None = None) -> bytes:
        if entry.note.startswith("unsupported"):
            raise UnsupportedError(entry.note)
        rec = self._record(entry.ref)
        if rec is None or rec.data is None:
            raise FsError(f"MFT record {entry.ref} is gone or unreadable")
        length = entry.size - offset if length is None else length
        return self._read_stream(rec.data, offset, max(0, min(length, entry.size - offset)))
