"""Signature carving: find files by content, no filesystem needed. The signature table is extensible."""
from __future__ import annotations

import hashlib
import os
import re
import struct
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from .device import SECTOR, BlockSource


@dataclass(frozen=True)
class Signature:
    name: str
    ext: str
    header: bytes
    footer: bytes | None = None
    max_size: int = 64 * 1024 * 1024
    footer_extra: int = 0  # bytes after the footer that belong to the file
    sizer: Callable[[BlockSource, int], int | None] | None = None  # exact size from the header, if the format has one
    header_offset: int = 0  # offset of `header` inside the file (e.g. MP4 'ftyp' sits at 4)


@dataclass
class CarvedFile:
    offset: int
    size: int
    sig: Signature
    sha256: str = ""

    @property
    def name(self) -> str:
        return f"{self.sig.ext}_{self.offset:012x}.{self.sig.ext}"


# ---------------------------------------------------------------- exact sizers
def _size_zip(src: BlockSource, off: int) -> int | None:
    """Find the end-of-central-directory record; the size is where it ends (bounded search)."""
    step = 4 * 1024 * 1024
    pos = off
    while pos < off + 512 * 1024 * 1024:
        data = src.read_at(pos, step + 22)
        if not data:
            return None
        i = data.find(b"PK\x05\x06")
        while i != -1 and i < step:
            tail = data[i:i + 22]
            if len(tail) == 22:
                comment = struct.unpack_from("<H", tail, 20)[0]
                cd_size, cd_off = struct.unpack_from("<II", tail, 12)
                # the central directory must end exactly where this end-of-directory record starts
                if off + cd_off + cd_size == pos + i:
                    return pos + i + 22 + comment - off
            i = data.find(b"PK\x05\x06", i + 1)
        pos += step
    return None


def _size_bmp(src: BlockSource, off: int) -> int | None:
    h = src.read_at(off, 14)
    if len(h) < 14:
        return None
    size = struct.unpack_from("<I", h, 2)[0]
    return size if 54 <= size <= 256 * 1024 * 1024 and h[6:10] == bytes(4) else None


def _size_sqlite(src: BlockSource, off: int) -> int | None:
    h = src.read_at(off, 100)
    if len(h) < 100:
        return None
    page = struct.unpack_from(">H", h, 16)[0]
    pages = struct.unpack_from(">I", h, 28)[0]
    page = 65536 if page == 1 else page
    if page < 512 or page & (page - 1) or not pages:
        return None
    return page * pages


def _size_mp4(src: BlockSource, off: int) -> int | None:
    """Walk top-level boxes; stop at the first thing that is not a plausible box."""
    pos, ok_boxes = off, 0
    while ok_boxes < 4096:
        h = src.read_at(pos, 16)
        if len(h) < 8:
            break
        size, kind = struct.unpack_from(">I4s", h, 0)
        if size == 1:
            if len(h) < 16:
                break
            size = struct.unpack_from(">Q", h, 8)[0]
        if size < 8 or not re.fullmatch(rb"[ -~]{4}", kind) or kind not in (
                b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"uuid", b"moof", b"mfra", b"pnot", b"meta"):
            break
        pos += size
        ok_boxes += 1
    return pos - off if ok_boxes >= 2 else None


def _size_riff(src: BlockSource, off: int) -> int | None:
    """RIFF container (WAV / AVI / WebP): total size is the 32-bit length at +4 plus the 8-byte header."""
    h = src.read_at(off, 12)
    if len(h) < 12:
        return None
    size = struct.unpack_from("<I", h, 4)[0] + 8
    return size if 44 <= size <= 4 * 1024 ** 3 else None


def _size_png(src: BlockSource, off: int) -> int | None:
    pos = off + 8
    for _ in range(100000):
        h = src.read_at(pos, 8)
        if len(h) < 8:
            return None
        ln, kind = struct.unpack(">I4s", h)
        pos += 12 + ln
        if kind == b"IEND":
            return pos - off
        if ln > 64 * 1024 * 1024 or not kind.isalpha():
            return None
    return None


SIGNATURES: list[Signature] = [
    Signature("JPEG", "jpg", b"\xff\xd8\xff", b"\xff\xd9", 32 * 1024 * 1024),
    Signature("PNG", "png", b"\x89PNG\r\n\x1a\n", None, 64 * 1024 * 1024, sizer=_size_png),
    Signature("GIF", "gif", b"GIF8", b"\x00\x3b", 32 * 1024 * 1024),
    Signature("PDF", "pdf", b"%PDF-", b"%%EOF", 256 * 1024 * 1024, footer_extra=1),
    Signature("ZIP/OOXML", "zip", b"PK\x03\x04", None, 512 * 1024 * 1024, sizer=_size_zip),
    Signature("MP4/MOV", "mp4", b"ftyp", None, 4 * 1024 ** 3, sizer=None, header_offset=4),
    Signature("BMP", "bmp", b"BM", None, 256 * 1024 * 1024, sizer=_size_bmp),
    Signature("SQLite", "sqlite", b"SQLite format 3\x00", None, 4 * 1024 ** 3, sizer=_size_sqlite),
    Signature("MP3 (ID3)", "mp3", b"ID3", None, 32 * 1024 * 1024),
    Signature("RAR", "rar", b"Rar!\x1a\x07", None, 512 * 1024 * 1024),
    Signature("7-Zip", "7z", b"7z\xbc\xaf\x27\x1c", None, 512 * 1024 * 1024),
    Signature("WAV audio", "wav", b"RIFF", None, 4 * 1024 ** 3, sizer=_size_riff),
    Signature("AVI video", "avi", b"RIFF", None, 4 * 1024 ** 3, sizer=_size_riff),
    Signature("WebP image", "webp", b"RIFF", None, 256 * 1024 * 1024, sizer=_size_riff),
]
_SIZERS = {"mp4": _size_mp4}


def office_kind(blob: bytes) -> str:
    """Tell DOCX/XLSX/PPTX from plain ZIP by looking at the first local-header names."""
    for marker, ext in ((b"word/", "docx"), (b"xl/", "xlsx"), (b"ppt/", "pptx")):
        if marker in blob[:8192]:
            return ext
    return "zip"


def _sizer_for(sig: Signature):
    return sig.sizer or _SIZERS.get(sig.ext)


def _validate_header(sig: Signature, data: bytes, i: int) -> bool:
    if sig.ext == "jpg":  # JPEG marker after SOI must be APPn/DQT/SOF/DHT etc.
        return i + 4 <= len(data) and data[i + 3] in (0xE0, 0xE1, 0xE2, 0xE8, 0xDB, 0xEE, 0xC0, 0xC4)
    if sig.ext == "gif":
        return data[i:i + 6] in (b"GIF87a", b"GIF89a")
    if sig.ext in ("wav", "avi", "webp"):  # all three start with RIFF; the form type tells them apart
        return data[i + 8:i + 12] == {"wav": b"WAVE", "avi": b"AVI ", "webp": b"WEBP"}[sig.ext]
    if sig.ext == "mp3":
        return i + 10 <= len(data) and data[i + 3] in (2, 3, 4) and data[i + 5] & 0x0F == 0
    return True


def carve(source: BlockSource, *, signatures: list[Signature] | None = None, chunk: int = 4 * 1024 * 1024,
          progress=None, cancel: Callable[[], bool] | None = None, start: int = 0) -> Iterator[CarvedFile]:
    """Yield carved files in offset order. Header hits are found with fast byte search; sizes come from the format
    (exact size, footer search, or max size as a last resort)."""
    sigs = signatures or SIGNATURES
    pos = start - start % SECTOR  # resume point: a cancelled carve of a huge disk continues here
    overlap = 64
    last_end = pos
    while pos < source.size:
        if cancel and cancel():
            return
        data = source.read_at(pos, chunk + overlap)
        if not data:
            break
        hits: list[tuple[int, Signature]] = []
        for sig in sigs:
            i = data.find(sig.header)
            while i != -1:
                start = i - sig.header_offset
                if start >= 0 and start < chunk and _validate_header(sig, data, start):
                    hits.append((pos + start, sig))
                i = data.find(sig.header, i + 1)
        for off, sig in sorted(hits, key=lambda h: h[0]):
            if off < last_end:
                continue
            size = _determine_size(source, off, sig)
            if size and size >= len(sig.header):
                last_end = off + size
                yield CarvedFile(off, size, sig)
        pos += chunk
        if progress:
            progress(min(pos, source.size), source.size)


def _determine_size(source: BlockSource, off: int, sig: Signature) -> int | None:
    sizer = _sizer_for(sig)
    if sizer:
        return sizer(source, off)
    if sig.footer:
        pos, window = off + len(sig.header), 1024 * 1024
        while pos < off + sig.max_size and pos < source.size:
            data = source.read_at(pos, window + len(sig.footer))
            if not data:
                break
            i = data.find(sig.footer)
            if i != -1:
                end = pos + i + len(sig.footer) + sig.footer_extra
                if sig.ext == "pdf":  # incremental updates append another xref + %%EOF
                    while True:
                        nxt = source.read_at(end, 64 * 1024)
                        j = nxt.find(b"%%EOF")
                        if j == -1 or b"startxref" not in nxt[:j]:
                            break
                        end += j + len(sig.footer) + 1
                return end - off
            pos += window
        return None
    return min(sig.max_size, source.size - off) if sig.ext in ("mp3", "rar", "7z") else None


def brand_ext(blob: bytes) -> str:
    """File extension for an ISO-BMFF file from its 'ftyp' major brand (MP4 / MOV / HEIC / 3GP / M4A)."""
    brand = blob[8:12]
    if brand in (b"heic", b"heix", b"mif1", b"msf1", b"hevc"):
        return "heic"
    if brand == b"qt  ":
        return "mov"
    if brand[:3] == b"3gp":
        return "3gp"
    if brand in (b"M4A ", b"M4B "):
        return "m4a"
    return "mp4"


def extract(source: BlockSource, cf: CarvedFile, out_dir: str) -> str:
    """Write one carved file into `out_dir` (the caller has already vetted the folder) and return its path."""
    os.makedirs(out_dir, exist_ok=True)
    head = source.read_at(cf.offset, 8192)
    ext = office_kind(head) if cf.sig.ext == "zip" else brand_ext(head) if cf.sig.ext == "mp4" else cf.sig.ext
    path = os.path.join(out_dir, f"{ext}_{cf.offset:012x}.{ext}")
    h = hashlib.sha256()
    with open(path, "wb") as f:
        done = 0
        while done < cf.size:
            block = source.read_at(cf.offset + done, min(1 << 20, cf.size - done))
            if not block:
                break
            f.write(block)
            h.update(block)
            done += len(block)
    cf.sha256 = h.hexdigest()
    return path
