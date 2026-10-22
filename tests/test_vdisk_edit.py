"""Virtual disk containers, disk editor, and the new CLI commands."""
import pytest
from conftest import payload, sha
from imgbuilders import Fat32Builder, NtfsBuilder, make_mbr_disk
from vbuilders import make_vdi, make_vhd_dynamic, make_vhdx, make_vmdk_sparse

from lavora.cli.main import main
from lavora.core import diskedit, vdisk
from lavora.core.device import BytesSource, open_source
from lavora.core.fs import open_filesystem


@pytest.fixture(scope="module")
def disk():
    ntfs = NtfsBuilder()
    ntfs.add_file("keep.txt", b"keep")
    ntfs.add_file("Photos/lost.jpg", payload(9, 40_000))
    ntfs.delete("Photos/lost.jpg")
    d, _ = make_mbr_disk([(ntfs.build(), 0x07)])
    return d


FORMATS = {"vhd": make_vhd_dynamic, "vhdx": make_vhdx, "vdi": make_vdi, "vmdk": make_vmdk_sparse}


@pytest.mark.parametrize("ext", FORMATS)
def test_container_reads_back_the_original_disk(ext, disk, tmp_path):
    p = tmp_path / f"d.{ext}"
    p.write_bytes(FORMATS[ext](disk))
    with open_source(str(p)) as src:
        assert src.size >= len(disk)
        assert src.read_at(0, len(disk)) == disk
        assert src.read_at(len(disk) - 100, 400)[:100] == disk[-100:]  # and zeros beyond the data, no error


@pytest.mark.parametrize("ext", FORMATS)
def test_deleted_file_recovery_works_through_a_container(ext, disk, tmp_path):
    p = tmp_path / f"d.{ext}"
    p.write_bytes(FORMATS[ext](disk))
    out = tmp_path / "out"
    assert main(["recover", str(p), "--out", str(out), "--all", "--deleted-only"]) == 0
    assert sha(next(out.rglob("lost.jpg")).read_bytes()) == sha(payload(9, 40_000))


def test_unallocated_blocks_read_as_zeros_and_the_file_is_smaller_than_the_disk(disk, tmp_path):
    big = disk + bytes(8 * (1 << 20))
    p = tmp_path / "s.vhd"
    p.write_bytes(make_vhd_dynamic(big))
    assert p.stat().st_size < len(big) / 2
    with open_source(str(p)) as src:
        assert src.read_at(len(disk) + 1000, 4096) == bytes(4096)


def test_flat_vmdk_descriptor(disk, tmp_path):
    (tmp_path / "x-flat.vmdk").write_bytes(disk)
    (tmp_path / "x.vmdk").write_text(f'# Disk DescriptorFile\nversion=1\ncreateType="monolithicFlat"\n\nRW {len(disk) // 512} FLAT "x-flat.vmdk" 0\n')
    with open_source(str(tmp_path / "x.vmdk")) as src:
        assert src.size == len(disk) and src.read_at(0, 1024) == disk[:1024]


def test_unsupported_variants_fail_with_a_clear_message(disk, tmp_path):
    raw = bytearray(make_vhd_dynamic(disk))
    raw[60:64] = (4).to_bytes(4, "big")  # differencing
    p = tmp_path / "diff.vhd"
    p.write_bytes(bytes(raw))
    with pytest.raises(vdisk.VirtualDiskError, match="parent"):
        open_source(str(p))
    comp = bytearray(make_vmdk_sparse(disk))
    comp[8:12] = (1 << 16).to_bytes(4, "little")
    q = tmp_path / "comp.vmdk"
    q.write_bytes(bytes(comp))
    with pytest.raises(vdisk.VirtualDiskError, match="compressed"):
        open_source(str(q))
    # a truncated container is damaged data, not an error: what is missing reads as zeros (recovery can still use the rest)
    (tmp_path / "t.vdi").write_bytes(make_vdi(disk)[:600])
    with open_source(str(tmp_path / "t.vdi")) as src:
        assert src.read_at(0, 4096) == bytes(4096)


def test_fixed_vhd_and_plain_images_still_open_as_before(disk, tmp_path):
    p = tmp_path / "plain.img"
    p.write_bytes(disk)
    with open_source(str(p)) as s:
        assert type(s).__name__ == "ImageSource" and s.size == len(disk)


# ------------------------------------------------------------------ disk editor
def test_hexview_and_describe(disk):
    src = BytesSource(disk)
    text = diskedit.hexview(src, 0, 512)
    assert text.splitlines()[-1].startswith("0000000001f0")
    assert diskedit.describe(disk[:512])["partitions"][0]["start_lba"] == 2048
    ntfs = diskedit.describe(disk[1 << 20:(1 << 20) + 512])
    assert ntfs["type"] == "NTFS boot sector" and ntfs["sectors_per_cluster"] == 8
    fat = Fat32Builder()
    assert diskedit.describe(fat.build()[:512])["type"] == "FAT32 boot sector"
    assert diskedit.describe(bytes(512))["type"] == "unknown"


def test_find_handles_boundaries_and_limits():
    data = bytearray(payload(1, 100_000).replace(b"N", b"M"))
    for off in (10, 4095, 65536 - 2, 99_990):
        data[off:off + 6] = b"NEEDLE"
    src = BytesSource(bytes(data))
    assert list(diskedit.find(src, b"NEEDLE", chunk=4096)) == [10, 4095, 65534, 99990]
    assert list(diskedit.find(src, b"NEEDLE", chunk=4096, limit=2)) == [10, 4095]
    assert list(diskedit.find(src, b"NEEDLE", start=5000, chunk=4096)) == [65534, 99990]
    assert list(diskedit.find(src, b"NEEDLE", cancel=lambda: True)) == []
    with pytest.raises(ValueError):
        list(diskedit.find(src, b""))


def test_sector_write_is_dry_run_by_default_and_undoable(tmp_path, disk):
    p = tmp_path / "e.img"
    p.write_bytes(disk)
    new = b"\xaa" * 512
    assert not diskedit.write_sector(str(p), 3, new).applied and p.read_bytes() == disk
    res = diskedit.write_sector(str(p), 3, new, dry_run=False)
    assert res.applied and p.read_bytes()[3 * 512:4 * 512] == new
    from lavora.core import undo
    undo.undo(res.undo_id)
    assert p.read_bytes() == disk


def test_cli_hex_find_and_sector_write(tmp_path, disk, capsys):
    p = tmp_path / "c.img"
    p.write_bytes(disk)
    assert main(["hex", str(p), "--lba", "0", "--describe"]) == 0
    out = capsys.readouterr().out
    assert "MBR / boot sector" in out and '"start_lba": 2048' in out
    assert main(["find", str(p), "--text", "NTFS    "]) == 0
    assert f"{(1 << 20) + 3}" in capsys.readouterr().out
    sector = tmp_path / "s.bin"
    sector.write_bytes(b"\x11" * 512)
    assert main(["sector-write", str(p), "--lba", "5", "--file", str(sector)]) == 0
    assert "dry run" in capsys.readouterr().out
    assert main(["sector-write", str(p), "--lba", "5", "--file", str(sector), "--apply"]) == 0
    assert "WRITTEN" in capsys.readouterr().out
    assert p.read_bytes()[5 * 512:6 * 512] == b"\x11" * 512
    sector.write_bytes(b"\x11" * 100)
    assert main(["sector-write", str(p), "--lba", "5", "--file", str(sector)]) == 2


def test_open_a_filesystem_inside_a_vhdx_end_to_end(disk, tmp_path):
    p = tmp_path / "vm.vhdx"
    p.write_bytes(make_vhdx(disk))
    with open_source(str(p)) as src:
        from lavora.core.device import PartitionView
        from lavora.core.partitions import read_partitions
        _s, parts = read_partitions(src)
        fs = open_filesystem(PartitionView(src, parts[0].start, parts[0].size))
        assert any(e.deleted and e.name == "lost.jpg" for e in fs.scan())
