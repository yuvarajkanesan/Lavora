"""Partition tables (MBR/GPT), lost-partition scanning, Doctor findings, undoable fixes."""
import struct

import pytest
from conftest import payload
from imgbuilders import SECTOR, Fat32Builder, NtfsBuilder, make_mbr_disk

from lavora.core import doctor, partitions, undo
from lavora.core.device import BytesSource, ImageSource, PartitionView
from lavora.core.fs import open_filesystem
from lavora.core.safety import SafetyError


@pytest.fixture
def two_volume_disk():
    ntfs = NtfsBuilder()
    ntfs.add_file("hello.txt", b"hello from ntfs")
    fat = Fat32Builder()
    fat.add_file("DATA.BIN", payload(1, 3000))
    disk, layout = make_mbr_disk([(ntfs.build(), 0x07), (fat.build(), 0x0C)], boot_code=b"\xfa\x33\xc0GRUB")
    return disk, layout


def test_mbr_roundtrip(two_volume_disk):
    disk, layout = two_volume_disk
    scheme, parts = partitions.read_partitions(BytesSource(disk))
    assert scheme == "MBR"
    assert [(p.start, p.size) for p in parts] == layout
    assert [p.fs for p in parts] == ["ntfs", "fat32"]


def test_build_mbr_matches_parse(two_volume_disk):
    disk, layout = two_volume_disk
    parts = [partitions.Partition(1, layout[0][0], layout[0][1], "x", fs="ntfs", bootable=True),
             partitions.Partition(2, layout[1][0], layout[1][1], "x", fs="fat32")]
    back = partitions.parse_mbr(partitions.build_mbr(parts))
    assert [(p.start, p.size) for p in back] == layout


def test_scan_finds_volumes_when_table_is_gone(two_volume_disk):
    disk, layout = two_volume_disk
    wiped = bytes(512) + disk[512:]
    found = partitions.scan_lost_partitions(BytesSource(wiped))
    assert [(f.start, f.fs) for f in found] == [(layout[0][0], "ntfs"), (layout[1][0], "fat32")]
    assert found[0].size == layout[0][1] and found[0].confidence == "high"


def test_scan_finds_ntfs_through_backup_boot_sector_when_start_is_destroyed(two_volume_disk):
    disk, layout = two_volume_disk
    d = bytearray(disk)
    start = layout[0][0]
    d[start:start + 16 * 1024] = bytes(16 * 1024)  # boot sector + beginning of the MFT region zeroed
    d[:512] = bytes(512)
    found = partitions.scan_lost_partitions(BytesSource(bytes(d)))
    ntfs = [f for f in found if f.fs == "ntfs"]
    assert len(ntfs) == 1 and ntfs[0].start == start and "backup" in ntfs[0].note


def test_fat32_backup_boot_sector_is_not_a_second_volume(two_volume_disk):
    disk, layout = two_volume_disk
    found = [f for f in partitions.scan_lost_partitions(BytesSource(disk)) if f.fs == "fat32"]
    assert len(found) == 1


def test_gpt_build_parse_and_repair():
    size = 8 * 1024 * 1024
    parts = [partitions.Partition(1, 1 << 20, 2 << 20, "x", fs="ntfs", name="Data"),
             partitions.Partition(2, 3 << 20, 1 << 20, "x", fs="ext", name="Linux")]
    prim, arr, bak = partitions.build_gpt_pair(size, parts)
    disk = bytearray(size)
    total = size // 512
    esect = len(arr) // 512
    disk[512:1024], disk[1024:1024 + len(arr)] = prim, arr
    disk[(total - 1 - esect) * 512:(total - 1) * 512], disk[(total - 1) * 512:] = arr, bak
    disk[:512] = partitions.build_mbr([partitions.Partition(1, 512, size - 1024, "x", fs="ntfs")])
    disk[450] = 0xEE  # protective MBR
    info = partitions.parse_gpt(BytesSource(bytes(disk)))
    assert info.primary_ok and info.backup_ok and len(info.entries) == 2
    assert info.entries[0].name == "Data" and info.entries[1].start == 3 << 20

    broken = bytearray(disk)
    broken[512 + 20] ^= 0xFF  # flip a bit in the primary header -> CRC mismatch
    src = BytesSource(bytes(broken))
    info = partitions.parse_gpt(src)
    assert not info.primary_ok and info.backup_ok and "CRC" in info.primary_error
    fixed = bytearray(broken)
    for off, data in partitions.gpt_repair_regions(src):
        fixed[off:off + len(data)] = data
    again = partitions.parse_gpt(BytesSource(bytes(fixed)))
    assert again.primary_ok and again.backup_ok and again.entries == info.entries


# ------------------------------------------------------------------ Doctor
def test_doctor_clean_disk_has_no_findings(two_volume_disk):
    disk, _ = two_volume_disk
    assert doctor.diagnose(BytesSource(disk)) == []


def test_doctor_rebuilds_mbr_and_fix_is_undoable(tmp_path, two_volume_disk, lavora_home):
    disk, layout = two_volume_disk
    broken = bytes(512) + disk[512:]
    path = tmp_path / "broken.img"
    path.write_bytes(broken)
    with ImageSource(str(path)) as src:
        findings = doctor.diagnose(src)
    assert findings[0].id == "no-table" and findings[0].severity == "critical"
    fix = findings[0].fixes[0]
    assert fix.id == "rebuild-mbr" and fix.auto

    dry = doctor.apply_fix(str(path), fix)  # dry run by default
    assert not dry.applied and path.read_bytes() == broken

    res = doctor.apply_fix(str(path), fix, dry_run=False)
    assert res.applied and res.undo_id
    with ImageSource(str(path)) as src:
        _s, parts = partitions.read_partitions(src)
        assert [(p.start, p.size) for p in parts] == layout
        assert doctor.diagnose(src) == []
    undo.undo(res.undo_id)
    assert path.read_bytes() == broken  # byte-for-byte back to the damaged state


def test_doctor_restores_ntfs_boot_sector_from_backup(tmp_path, two_volume_disk):
    disk, layout = two_volume_disk
    start = layout[0][0]
    d = bytearray(disk)
    d[start:start + 512] = bytes(512)
    path = tmp_path / "ntfs-damaged.img"
    path.write_bytes(bytes(d))
    with ImageSource(str(path)) as src:
        findings = doctor.diagnose(src)
    f = next(x for x in findings if x.id.startswith("ntfs-boot"))
    assert f.severity == "critical"
    doctor.apply_fix(str(path), f.fixes[0], dry_run=False)
    assert path.read_bytes() == disk  # identical to the original again
    with ImageSource(str(path)) as src:
        assert open_filesystem(PartitionView(src, start, layout[0][1])).name == "ntfs"


def test_doctor_restores_fat32_boot_sector_from_sector_6(tmp_path, two_volume_disk):
    disk, layout = two_volume_disk
    start = layout[1][0]
    d = bytearray(disk)
    d[start:start + 512] = bytes(512)
    path = tmp_path / "fat-damaged.img"
    path.write_bytes(bytes(d))
    with ImageSource(str(path)) as src:
        f = next(x for x in doctor.diagnose(src) if x.id.startswith("fat32-boot"))
    doctor.apply_fix(str(path), f.fixes[0], dry_run=False)
    assert path.read_bytes() == disk


def test_doctor_reports_damaged_mft_as_plan_only(two_volume_disk):
    disk, layout = two_volume_disk
    d = bytearray(disk)
    n = NtfsBuilder()
    mft = layout[0][0] + n.mft_lcn * n.cs
    d[mft:mft + 4] = b"XXXX"
    findings = doctor.diagnose(BytesSource(bytes(d)))
    f = next(x for x in findings if x.id.startswith("ntfs-mft"))
    assert not f.fixes[0].auto and "chkdsk" in f.fixes[0].commands[0]
    with pytest.raises(ValueError):
        doctor.apply_fix("whatever", f.fixes[0], dry_run=False)


def test_doctor_gpt_primary_damage(tmp_path):
    size = 8 * 1024 * 1024
    parts = [partitions.Partition(1, 1 << 20, 2 << 20, "x", fs="ntfs")]
    prim, arr, bak = partitions.build_gpt_pair(size, parts)
    d = bytearray(size)
    total, es = size // 512, len(arr) // 512
    d[512:1024], d[1024:1024 + len(arr)] = prim, arr
    d[(total - 1 - es) * 512:(total - 1) * 512], d[(total - 1) * 512:] = arr, bak
    d[:512] = partitions.build_mbr([partitions.Partition(1, 512, size - 1024, "x")])
    d[450] = 0xEE
    d[512:1024] = bytes(512)
    path = tmp_path / "gpt.img"
    path.write_bytes(bytes(d))
    with ImageSource(str(path)) as src:
        f = next(x for x in doctor.diagnose(src) if x.id == "gpt-copy")
    doctor.apply_fix(str(path), f.fixes[0], dry_run=False)
    with ImageSource(str(path)) as src:
        info = partitions.parse_gpt(src)
        assert info.primary_ok and info.backup_ok


def test_writes_must_be_sector_aligned(tmp_path):
    p = tmp_path / "x.img"
    p.write_bytes(bytes(4096))
    with pytest.raises(SafetyError):
        undo.write_regions(str(p), [(100, b"abc")], "bad", dry_run=False)


def test_snapshot_list_and_undo_restore_exact_bytes(tmp_path):
    p = tmp_path / "x.img"
    original = payload(9, 8192)
    p.write_bytes(original)
    res = undo.write_regions(str(p), [(512, b"\xaa" * 1024)], "test write", dry_run=False)
    assert p.read_bytes() != original
    assert [s["id"] for s in undo.list_snapshots()] == [res.undo_id]
    undo.undo(res.undo_id)
    assert p.read_bytes() == original


def test_struct_used():
    assert struct.calcsize("<H") == 2 and SECTOR == 512
