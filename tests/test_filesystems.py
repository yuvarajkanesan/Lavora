"""Deleted-file scan and recovery against generated FAT32, exFAT, NTFS and ext images (SHA-256 must match)."""
import struct

import pytest
from conftest import payload, sha
from imgbuilders import ExfatBuilder, ExtBuilder, Fat32Builder, NtfsBuilder

from lavora.core.device import BytesSource
from lavora.core.fs import FsError, UnsupportedError, open_filesystem
from lavora.core.fs.ext import ExtFilesystem
from lavora.core.fs.fat import ExfatFilesystem, FatFilesystem
from lavora.core.fs.ntfs import NtfsFilesystem


def by_path(fs, **kw):
    return {e.path: e for e in fs.scan(**kw)}


def read_all(fs, e):
    return fs.read(e, 0, e.size)


# ------------------------------------------------------------------ FAT32
@pytest.fixture
def fat32():
    b = Fat32Builder()
    data = {
        "docs/Holiday Photo.jpg": payload(1, 5000),
        "docs/report.docx": payload(2, 1200),
        "NOTES.TXT": b"keep me\n" * 10,
        "big.bin": payload(3, 40_000),
    }
    for p, d in data.items():
        b.add_file(p, d)
    b.delete("docs/Holiday Photo.jpg")
    b.delete("big.bin")
    return b, data


def test_fat32_lists_live_and_deleted(fat32):
    b, data = fat32
    fs = open_filesystem(BytesSource(b.build()))
    assert isinstance(fs, FatFilesystem) and fs.kind == "fat32"
    files = by_path(fs)
    assert files["NOTES.TXT"].deleted is False
    assert files["docs/report.docx"].deleted is False
    gone = files["docs/Holiday Photo.jpg"]  # long name survives through its LFN entries
    assert gone.deleted and gone.size == 5000 and gone.overwritten_pct == 0


def test_fat32_recovers_deleted_files_bit_exact(fat32):
    b, data = fat32
    fs = open_filesystem(BytesSource(b.build()))
    files = by_path(fs)
    assert sha(read_all(fs, files["docs/Holiday Photo.jpg"])) == sha(data["docs/Holiday Photo.jpg"])
    big = next(e for e in files.values() if e.deleted and e.size == 40_000)
    assert sha(read_all(fs, big)) == sha(data["big.bin"])  # deleted entry of a plain "BIG.BIN"/"big.bin"
    assert sha(read_all(fs, files["docs/report.docx"])) == sha(data["docs/report.docx"])


def test_fat32_detects_overwritten_clusters():
    b = Fat32Builder()
    b.add_file("old.dat", payload(4, 3000))
    b.delete("old.dat")
    b.add_file("new.dat", payload(5, 3000))  # first-fit allocator reuses the freed clusters
    fs = open_filesystem(BytesSource(b.build()))
    old = next(e for e in fs.scan() if e.deleted)
    assert old.overwritten_pct == 100.0 and not old.recoverable


def test_fat32_deleted_only_filter(fat32):
    b, _ = fat32
    fs = open_filesystem(BytesSource(b.build()))
    assert all(e.deleted for e in fs.scan(include_live=False))


# ------------------------------------------------------------------ exFAT
def test_exfat_scan_and_recover():
    b = ExfatBuilder()
    keep = payload(10, 9000)
    gone = payload(11, 20000)
    chained = payload(12, 10000)
    b.add_file("keep.bin", keep)
    b.add_file("pics/Vacation 2024.png", gone)
    b.add_file("chained.bin", chained, nofat=False)
    b.delete("pics/Vacation 2024.png")
    fs = open_filesystem(BytesSource(b.build()))
    assert isinstance(fs, ExfatFilesystem)
    files = by_path(fs)
    assert sha(read_all(fs, files["keep.bin"])) == sha(keep)
    assert sha(read_all(fs, files["chained.bin"])) == sha(chained)
    d = files["pics/Vacation 2024.png"]
    assert d.deleted and d.overwritten_pct == 0
    assert sha(read_all(fs, d)) == sha(gone)


def test_exfat_overwrite_detected():
    b = ExfatBuilder()
    b.add_file("a.bin", payload(1, 5000))
    b.delete("a.bin")
    b.add_file("b.bin", payload(2, 5000))
    fs = open_filesystem(BytesSource(b.build()))
    a = next(e for e in fs.scan() if e.deleted)
    assert a.overwritten_pct == 100.0


# ------------------------------------------------------------------ NTFS
@pytest.fixture
def ntfs():
    b = NtfsBuilder()
    data = {
        "tiny.txt": b"resident file content",
        "Documents/budget.xlsx": payload(20, 30_000),
        "Documents/frag.bin": payload(21, 50_000),
        "Photos/img.jpg": payload(22, 12_345),
        "Photos/lost.jpg": payload(23, 70_000),
        "small_deleted.txt": b"deleted resident text " * 8,
    }
    b.add_file("tiny.txt", data["tiny.txt"])
    b.add_file("Documents/budget.xlsx", data["Documents/budget.xlsx"])
    b.add_file("Documents/frag.bin", data["Documents/frag.bin"], fragments=3)
    b.add_file("Photos/img.jpg", data["Photos/img.jpg"])
    b.add_file("Photos/lost.jpg", data["Photos/lost.jpg"])
    b.add_file("small_deleted.txt", data["small_deleted.txt"])
    b.delete("Photos/lost.jpg")
    b.delete("small_deleted.txt")
    b.delete("Documents/frag.bin")
    return b, data


def test_ntfs_lists_paths_and_deleted_flag(ntfs):
    b, data = ntfs
    fs = open_filesystem(BytesSource(b.build()))
    assert isinstance(fs, NtfsFilesystem) and fs.record_size == 1024
    files = by_path(fs)
    assert not files["Documents/budget.xlsx"].deleted
    assert files["Photos/lost.jpg"].deleted and files["Photos/lost.jpg"].size == 70_000
    assert files["small_deleted.txt"].deleted
    assert not any(p.startswith("$") for p in files)  # metadata files hidden


def test_ntfs_recovery_is_bit_exact(ntfs):
    b, data = ntfs
    fs = open_filesystem(BytesSource(b.build()))
    files = by_path(fs)
    for path in ("tiny.txt", "Documents/budget.xlsx", "Documents/frag.bin", "Photos/lost.jpg", "small_deleted.txt"):
        assert sha(read_all(fs, files[path])) == sha(data[path]), path


def test_ntfs_fragmented_file_has_multiple_runs_and_reads_correctly(ntfs):
    b, data = ntfs
    fs = open_filesystem(BytesSource(b.build()))
    e = by_path(fs)["Documents/frag.bin"]
    rec = fs._record(e.ref)
    assert len(rec.data.runs) == 3
    assert read_all(fs, e) == data["Documents/frag.bin"]


def test_ntfs_partial_reads(ntfs):
    b, data = ntfs
    fs = open_filesystem(BytesSource(b.build()))
    e = by_path(fs)["Photos/lost.jpg"]
    assert fs.read(e, 4090, 20) == data["Photos/lost.jpg"][4090:4110]


def test_ntfs_overwritten_clusters_reported():
    b = NtfsBuilder()
    b.add_file("a.bin", payload(1, 20_000))
    b.delete("a.bin")
    b.add_file("b.bin", payload(2, 20_000))  # takes the freed clusters
    fs = open_filesystem(BytesSource(b.build()))
    a = by_path(fs)["a.bin"]
    assert a.deleted and a.overwritten_pct == 100.0


def test_ntfs_sparse_file_reads_zero_hole():
    b = NtfsBuilder()
    head, tail = b"H" * 4096, b"T" * 4096
    b.add_sparse("sparse.bin", head, 2, tail)
    fs = open_filesystem(BytesSource(b.build()))
    e = by_path(fs)["sparse.bin"]
    data = read_all(fs, e)
    assert data == head + bytes(8192) + tail


def test_ntfs_compressed_file_is_flagged_unsupported():
    b = NtfsBuilder()
    b.add_file("c.bin", payload(3, 9000), flags=0x0001)
    fs = open_filesystem(BytesSource(b.build()))
    e = by_path(fs)["c.bin"]
    assert e.note.startswith("unsupported")
    with pytest.raises(UnsupportedError):
        fs.read(e, 0, 10)


def test_ntfs_rejects_damaged_boot_sector():
    img = bytearray(NtfsBuilder().build())
    img[3:11] = b"\0" * 8
    with pytest.raises(FsError):
        open_filesystem(BytesSource(bytes(img)))


def test_ntfs_torn_record_is_skipped_not_fatal(ntfs):
    b, _ = ntfs
    img = bytearray(b.build())
    off = b.mft_lcn * b.cs + 17 * b.REC  # break the fixup of record 17
    img[off + 510:off + 512] = b"\xde\xad"
    fs = open_filesystem(BytesSource(bytes(img)))
    assert len(list(fs.scan())) >= 3


# ------------------------------------------------------------------ ext
@pytest.mark.parametrize("extents", [True, False], ids=["ext4-extents", "ext2-blockmap"])
def test_ext_live_and_deleted_recovery(extents):
    b = ExtBuilder(extents=extents)
    keep, gone, big = payload(30, 4000), payload(31, 9000), payload(32, 20_000)
    b.add_file("keep.txt", keep)
    b.add_file("photos/gone.jpg", gone)
    b.add_file("big.bin", big)
    b.delete("photos/gone.jpg")
    b.delete("big.bin")
    fs = open_filesystem(BytesSource(b.build()))
    assert isinstance(fs, ExtFilesystem)
    files = by_path(fs)
    assert sha(read_all(fs, files["keep.txt"])) == sha(keep)
    g = files["photos/gone.jpg"]  # name recovered from directory slack
    assert g.deleted and g.size == 9000 and g.overwritten_pct == 0
    assert sha(read_all(fs, g)) == sha(gone)
    assert sha(read_all(fs, files["big.bin"])) == sha(big)


def test_ext_overwrite_detected():
    b = ExtBuilder()
    b.add_file("a.bin", payload(1, 5000))
    b.delete("a.bin")
    b.add_file("b.bin", payload(2, 5000))
    fs = open_filesystem(BytesSource(b.build()))
    a = next(e for e in fs.scan() if e.deleted)
    assert a.overwritten_pct == 100.0


def test_unknown_filesystem_is_a_clear_error():
    with pytest.raises(FsError):
        open_filesystem(BytesSource(bytes(4096)))


def test_struct_import_used():
    assert struct.calcsize("<I") == 4
