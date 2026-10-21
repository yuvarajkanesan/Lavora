"""Signature carving: seeded files at known offsets inside noise, no filesystem."""
import io
import random
import sqlite3
import struct
import zipfile

import pytest
from conftest import sha

from lavora.core import carver
from lavora.core.device import BytesSource


def jpeg(n=3000, seed=1):
    body = random.Random(seed).randbytes(n).replace(b"\xff", b"\xfe")  # no accidental markers
    return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + body + b"\xff\xd9"


def png(n=2000, seed=2):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + b"\0\0\0\0"
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", bytes(13)) + chunk(b"IDAT", random.Random(seed).randbytes(n)) + chunk(b"IEND", b"")


def gif(seed=3):
    return b"GIF89a" + random.Random(seed).randbytes(500).replace(b"\x3b", b"\x3a") + b"\x00\x3b"


def pdf(seed=4):
    return b"%PDF-1.4\n" + random.Random(seed).randbytes(800).replace(b"%", b"x") + b"\nstartxref\n0\n%%EOF\n"


def office(kind="docx"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr(("word/" if kind == "docx" else "xl/") + "document.xml", "<doc>" + "hello " * 300 + "</doc>")
    return buf.getvalue()


def mp4(seed=5):
    ftyp = struct.pack(">I4s", 24, b"ftyp") + b"isom" + bytes(4) + b"isomiso2"
    moov = struct.pack(">I4s", 108, b"moov") + random.Random(seed).randbytes(100)
    mdat = struct.pack(">I4s", 1008, b"mdat") + random.Random(seed + 1).randbytes(1000)
    return ftyp + moov + mdat


def sqlite_db(tmp_path):
    p = tmp_path / "t.db"
    con = sqlite3.connect(p)
    con.execute("create table t(a, b)")
    con.executemany("insert into t values (?, ?)", [(i, "x" * 100) for i in range(300)])
    con.commit()
    con.close()
    return p.read_bytes()


def bmp(seed=6):
    body = random.Random(seed).randbytes(2000)
    return b"BM" + struct.pack("<IHHI", 54 + len(body), 0, 0, 54) + bytes(40) + body


def make_disk(items, size=2 * 1024 * 1024, seed=99):
    rnd = random.Random(seed)
    disk = bytearray(rnd.randbytes(size))
    # scrub accidental signatures from the noise
    for sig in (b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"%PDF-", b"PK\x03\x04", b"ftyp", b"SQLite", b"ID3", b"BM"):
        i = disk.find(sig)
        while i != -1:
            disk[i] = 0x01
            i = disk.find(sig, i + 1)
    placed = []
    for off, data, ext in items:
        disk[off:off + len(data)] = data
        placed.append((off, len(data), ext, sha(data)))
    return bytes(disk), placed


def test_carves_each_format_at_known_offsets(tmp_path):
    items = [(4096, jpeg(), "jpg"), (50_000, png(), "png"), (100_000, gif(), "gif"), (150_000, pdf(), "pdf"),
             (200_000, office("docx"), "zip"), (260_000, mp4(), "mp4"), (300_000, sqlite_db(tmp_path), "sqlite"),
             (700_000, bmp(), "bmp")]
    disk, placed = make_disk(items)
    src = BytesSource(disk)
    found = {c.offset: c for c in carver.carve(src)}
    for off, size, ext, digest in placed:
        assert off in found, f"{ext} not found at {off}"
        c = found[off]
        assert c.sig.ext == ext
        assert c.size == size, f"{ext}: size {c.size} != {size}"
        assert sha(disk[off:off + c.size]) == digest


def test_extract_writes_files_and_classifies_office_documents(tmp_path):
    items = [(8192, office("docx"), "zip"), (80_000, office("xlsx"), "zip"), (160_000, jpeg(), "jpg")]
    disk, placed = make_disk(items)
    src = BytesSource(disk)
    out = tmp_path / "out"
    names = []
    for c in carver.carve(src):
        names.append(carver.extract(src, c, str(out)).rsplit(".", 1)[1])
    assert sorted(names) == ["docx", "jpg", "xlsx"]
    for p in out.iterdir():
        assert len(p.read_bytes()) > 0


def test_files_spanning_chunk_boundaries_are_found():
    j = jpeg(6000)
    disk, placed = make_disk([(1024 - 3, j, "jpg")], size=64 * 1024)
    found = list(carver.carve(BytesSource(disk), chunk=1024))
    assert len(found) == 1 and found[0].offset == 1021 and found[0].size == len(j)


def test_type_filter_and_custom_signature():
    disk, _ = make_disk([(4096, jpeg(), "jpg"), (60_000, png(), "png")], size=256 * 1024)
    only_png = [s for s in carver.SIGNATURES if s.ext == "png"]
    assert [c.sig.ext for c in carver.carve(BytesSource(disk), signatures=only_png)] == ["png"]
    custom = carver.Signature("Lavora test", "lvt", b"LVT1", b"END!", 4096)
    blob = b"LVT1" + b"payload" * 20 + b"END!"
    disk2, _ = make_disk([(10_000, blob, "lvt")], size=128 * 1024)
    got = list(carver.carve(BytesSource(disk2), signatures=[custom]))
    assert got[0].offset == 10_000 and got[0].size == len(blob)


def test_cancel_stops_early():
    disk, _ = make_disk([(4096, jpeg(), "jpg")], size=256 * 1024)
    assert list(carver.carve(BytesSource(disk), chunk=4096, cancel=lambda: True)) == []


@pytest.mark.parametrize("junk", [b"", b"\xff\xd8\xff\xe0"])
def test_truncated_or_empty_input_is_safe(junk):
    assert list(carver.carve(BytesSource(junk))) == []
