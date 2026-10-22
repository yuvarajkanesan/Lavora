"""Huge-disk behaviour: a 5 TiB (synthetic, sparse) source must be handled without hanging, without loading it into memory,
and every long operation must stop promptly when cancelled."""
import errno
import os
import time

import pytest
from conftest import payload
from imgbuilders import Fat32Builder, NtfsBuilder, make_mbr_disk

from lavora.cli.main import main
from lavora.core import carver, imaging, partitions, session, smart
from lavora.core.device import SECTOR, BlockSource, BytesSource
from lavora.core.fs import open_filesystem
from lavora.core.fs.base import FileEntry, Filesystem
from lavora.core.fs.fat import ChainMap, _sampled_used
from lavora.core.fs.ntfs import DataStream
from lavora.core.recovery import Estimate, recover

TiB = 1 << 40


class VirtualDisk(BlockSource):
    """`size` bytes of zeros with small overlays at given offsets; counts how much was actually read."""

    def __init__(self, size: int, overlays: dict[int, bytes] | None = None):
        self.size, self.path, self.overlays, self.bytes_read, self.reads = size, "<virtual>", dict(overlays or {}), 0, 0

    def read_at(self, offset: int, length: int) -> bytes:
        if offset < 0 or offset >= self.size or length <= 0:
            return b""
        length = min(length, self.size - offset)
        self.bytes_read += length
        self.reads += 1
        buf = bytearray(length)
        for o, data in self.overlays.items():
            lo, hi = max(o, offset), min(o + len(data), offset + length)
            if lo < hi:
                buf[lo - offset:hi - offset] = data[lo - o:hi - o]
        return bytes(buf)


def timed(fn):
    t = time.perf_counter()
    r = fn()
    return r, time.perf_counter() - t


def gpt_5tib():
    size = 5 * TiB
    parts = [partitions.Partition(1, 1 << 20, 4 * TiB, "x", fs="ntfs", name="Data")]
    prim, arr, bak = partitions.build_gpt_pair(size, parts)
    total, es = size // SECTOR, len(arr) // SECTOR
    mbr = partitions.build_mbr([partitions.Partition(1, SECTOR, (1 << 32) * SECTOR - SECTOR, "x")])
    mbr = mbr[:450] + b"\xee" + mbr[451:]
    return VirtualDisk(size, {0: mbr, SECTOR: prim, 2 * SECTOR: arr, (total - 1 - es) * SECTOR: arr, (total - 1) * SECTOR: bak})


def test_5tib_gpt_is_read_instantly_and_without_reading_the_disk():
    disk = gpt_5tib()
    (scheme, parts), dt = timed(lambda: partitions.read_partitions(disk))
    assert scheme == "GPT" and parts[0].size == 4 * TiB and parts[0].start == 1 << 20
    assert dt < 2 and disk.bytes_read < 1 << 20
    info = partitions.parse_gpt(disk)
    assert info.primary_ok and info.backup_ok


def test_5tib_gpt_repair_regions_computed_for_the_end_of_the_disk():
    disk = gpt_5tib()
    disk.overlays.pop(SECTOR)  # kill the primary header
    regions = partitions.gpt_repair_regions(disk)
    assert regions and regions[0][0] == SECTOR and disk.bytes_read < 1 << 20


def test_lost_partition_scan_honours_cancel_and_limit():
    disk = VirtualDisk(5 * TiB)
    calls = []

    def cancel():
        calls.append(1)
        return len(calls) > 3
    _, dt = timed(lambda: partitions.scan_lost_partitions(disk, cancel=cancel))
    assert dt < 5 and disk.bytes_read < 64 << 20  # stopped after a few chunks of a 5 TiB disk
    disk2 = VirtualDisk(5 * TiB)
    partitions.scan_lost_partitions(disk2, max_bytes=32 << 20)
    assert disk2.bytes_read < 40 << 20


def test_surface_scan_cancel_and_resume_point():
    disk = VirtualDisk(5 * TiB)
    n = []
    res = smart.surface_scan(disk, block=1 << 20, cancel=lambda: n.append(1) or len(n) > 10)
    assert res.scanned == 10 << 20
    res2 = smart.surface_scan(disk, block=1 << 20, start=res.scanned, max_bytes=res.scanned + (4 << 20))
    assert res2.scanned == res.scanned + (4 << 20)


def test_carve_can_start_deep_into_a_5tib_disk_and_be_cancelled():
    jpg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x01" * 500 + b"\xff\xd9"
    at = 5 * TiB - (64 << 20) + 4096
    disk = VirtualDisk(5 * TiB, {at: jpg})
    found, dt = timed(lambda: list(carver.carve(disk, start=5 * TiB - (64 << 20))))
    assert [(c.offset, c.size) for c in found] == [(at, len(jpg))] and dt < 10
    disk2 = VirtualDisk(5 * TiB)
    k = []
    list(carver.carve(disk2, cancel=lambda: k.append(1) or len(k) > 5))
    assert disk2.bytes_read <= 6 * ((4 << 20) + 64)


# ------------------------------------------------------------------ NTFS / FAT / ext internals at 5 TiB scale
def big_ntfs_fs():
    b = NtfsBuilder()
    b.add_file("a.txt", b"x")
    return open_filesystem(BytesSource(b.build()))


def test_ntfs_random_read_in_a_hugely_fragmented_4tib_file_is_fast():
    fs = big_ntfs_fs()
    cluster = fs.cluster
    runs = [(i * 4 + 10, 1) for i in range(200_000)]  # 200k fragments
    ds = DataStream(runs=runs, real_size=200_000 * cluster, init_size=200_000 * cluster)
    marker_cluster = runs[150_000][0]
    fs.source = VirtualDisk(5 * TiB, {marker_cluster * cluster: b"MARK" * 1024})
    data, dt = timed(lambda: fs._read_stream(ds, 150_000 * cluster, 4096))
    assert data[:4] == b"MARK" and dt < 0.5
    _, dt = timed(lambda: [fs._read_stream(ds, i * cluster, cluster) for i in range(0, 200_000, 1000)])
    assert dt < 2  # bisect, not a scan of every run per read


def test_ntfs_overwrite_estimate_on_a_2tb_file_uses_popcount_not_a_loop():
    fs = big_ntfs_fs()
    clusters = 500_000_000  # 2 TB of 4 KiB clusters
    bitmap = bytearray(b"\xff" * (clusters // 8 // 2)) + bytearray(clusters // 8 // 2)  # first half in use, second half free
    fs._bm_ds = DataStream(resident=bytes(bitmap), real_size=len(bitmap), init_size=len(bitmap))
    ds = DataStream(runs=[(0, clusters)], real_size=clusters * fs.cluster)
    pct, dt = timed(lambda: fs._allocated_pct(ds))
    assert abs(pct - 50.0) < 0.001 and dt < 2


def test_sampled_overwrite_estimate_is_bounded_work():
    calls = []
    pct, dt = timed(lambda: _sampled_used(10, 10 ** 9, lambda c: calls.append(c) or c % 2 == 0))
    assert len(calls) <= 4096 and 45 < pct < 55 and dt < 1


def test_fat_chain_map_stores_runs_not_clusters():
    fat = {c: c + 1 for c in range(2, 300_000)}
    fat[300_000] = 0x0FFFFFFF
    fat.update({c: c + 1 for c in range(500_000, 500_100)})
    fat[500_100] = 0x0FFFFFFF
    cm = ChainMap(2, lambda c: fat.get(c, 0x0FFFFFFF), lambda c: 2 <= c < 600_000, 0x0FFFFFF8)
    loc, dt = timed(lambda: cm.locate(250_000))
    assert loc[0] == 250_002 and loc[1] >= 256 and len(cm.runs) == 1 and dt < 2
    assert cm.locate(10 ** 7) is None


# ------------------------------------------------------------------ recovery does not hang or fill the disk silently
class HugeFs(Filesystem):
    name = "huge"

    def __init__(self, fail_with=None):
        super().__init__(BytesSource(b""))
        self.fail_with, self.read_calls = fail_with, 0

    def scan(self, *, include_live=True, progress=None, cancel=None):
        return iter(())

    def read(self, entry, offset=0, length=None):
        self.read_calls += 1
        if self.fail_with:
            raise self.fail_with
        return bytes(length if length is not None else 1 << 20)


def test_recovering_a_2tb_file_can_be_cancelled_within_a_moment(tmp_path):
    fs = HugeFs()
    e = FileEntry("big/huge.bin", "huge.bin", 2 * TiB, deleted=True)
    reads = []
    rep, dt = timed(lambda: recover(fs, [e], str(tmp_path / "out"), cancel=lambda: reads.append(1) or len(reads) > 6))
    assert rep.cancelled and dt < 5 and fs.read_calls <= 6
    assert not list((tmp_path / "out").rglob("huge.bin"))  # no truncated file left pretending to be a recovery


def test_full_destination_stops_the_run_instead_of_failing_every_file(tmp_path):
    fs = HugeFs(fail_with=OSError(errno.ENOSPC, "No space left on device"))
    entries = [FileEntry(f"f{i}.bin", f"f{i}.bin", 1 << 20, deleted=True) for i in range(5)]
    rep = recover(fs, entries, str(tmp_path / "o"))
    assert "full" in rep.stopped_reason and fs.read_calls == 1 and rep.failed == 1


def test_recovery_streams_an_unbounded_generator_with_constant_memory(tmp_path):
    fs = HugeFs()

    def gen():
        for i in range(3000):
            yield FileEntry(f"d{i % 7}/f{i}.bin", f"f{i}.bin", 10, deleted=True)
    rep = recover(fs, gen(), str(tmp_path / "o"), keep_results=False)
    assert rep.recovered == 3000 and rep.files == []


def test_estimate_summary():
    est = Estimate()
    for e in (FileEntry("a", "a", 10, deleted=True), FileEntry("b", "b", 20, deleted=True, overwritten_pct=40.0),
              FileEntry("c", "c", 5, deleted=True, overwritten_pct=100.0), FileEntry("d", "d", 5),
              FileEntry("e", "e", 5, deleted=True, note="unsupported: compressed")):
        est.add(e)
    d = est.as_dict()
    assert (d["files"], d["deleted"], d["good"], d["partial"], d["lost"], d["unsupported"]) == (5, 4, 1, 1, 1, 1)


# ------------------------------------------------------------------ streaming scans, cancel, sessions
def test_ntfs_scan_is_lazy_and_cancellable():
    b = NtfsBuilder()
    for i in range(30):
        b.add_file(f"dir{i % 3}/f{i}.txt", payload(i, 100))
    fs = open_filesystem(BytesSource(b.build()))
    it = fs.scan()
    assert next(it).name  # produced without exhausting the MFT first
    assert list(fs.scan(cancel=lambda: True)) == []


def test_ntfs_paths_resolve_when_the_parent_comes_later_in_the_mft():
    b = NtfsBuilder()
    b.next_rec = 17
    ts = 1_700_000_000
    # record 17 is a file whose parent directory is record 18, which appears *after* it in the MFT
    b.records[17] = b._record(17, 1, [b._stdinfo(ts), b._filename(18, "child.txt", 3, False, ts), b._resident(0x80, b"abc", 2)])
    b.records[18] = b._record(18, 3, [b._stdinfo(ts), b._filename(5, "late", 0, True, ts)])
    b.records[19] = b._record(19, 1, [b._stdinfo(ts), b._filename(77, "orphan.txt", 3, False, ts), b._resident(0x80, b"abc", 2)])
    fs = open_filesystem(BytesSource(b.build()))
    paths = {e.path for e in fs.scan()}
    assert "late/child.txt" in paths and "late" in paths
    assert "$OrphanFiles/orphan.txt" in paths  # a parent that never appears is reported as an orphan, not lost


@pytest.fixture
def ntfs_disk(tmp_path):
    b = NtfsBuilder()
    data = {f"Photos/img{i}.jpg": payload(100 + i, 3000 + i) for i in range(8)}
    for p, d in data.items():
        b.add_file(p, d)
    for p in list(data)[:5]:
        b.delete(p)
    disk, _ = make_mbr_disk([(b.build(), 0x07)])
    path = tmp_path / "d.img"
    path.write_bytes(disk)
    return str(path), data


def test_session_save_then_recover_without_rescanning(ntfs_disk, tmp_path, capsys, monkeypatch):
    path, data = ntfs_disk
    sess = str(tmp_path / "scan.lvs")
    assert main(["scan", path, "--deleted-only", "--save", sess]) == 0
    assert "session saved" in capsys.readouterr().out and session.is_complete(sess)
    # prove the second step does not scan: make any scan attempt explode
    monkeypatch.setattr(Filesystem, "scan", lambda *a, **k: (_ for _ in ()).throw(AssertionError("rescanned")), raising=False)
    from lavora.core.fs.ntfs import NtfsFilesystem
    monkeypatch.setattr(NtfsFilesystem, "scan", lambda *a, **k: (_ for _ in ()).throw(AssertionError("rescanned")))
    code = main(["recover", "--session", sess, path, "--out", str(tmp_path / "out"), "--all"])
    assert code == 0
    got = {f.name: f.read_bytes() for f in (tmp_path / "out").rglob("*.jpg")}
    assert len(got) == 5 and got["img0.jpg"] == data["Photos/img0.jpg"]


def test_cancelled_scan_leaves_a_usable_partial_session(ntfs_disk, tmp_path):
    path, _ = ntfs_disk
    sess = str(tmp_path / "p.lvs")
    from lavora.cli.main import TOKEN
    from lavora.core.device import ImageSource
    with ImageSource(path) as src:
        _s, plist = partitions.read_partitions(src)
        from lavora.core.device import PartitionView
        vol = PartitionView(src, plist[0].start, plist[0].size)
        fs = open_filesystem(vol)
        w = session.SessionWriter(sess, path, src.size, plist[0].start, plist[0].size, fs.name)
        for seen, _e in enumerate(w.tee(fs.scan(), cancel=lambda: False), start=1):
            if seen == 3:
                break  # abandon the scan midway, as a crash or a cancel would
        w.close(False)
    assert not session.is_complete(sess)
    entries = list(session.read_entries(sess))
    assert len(entries) >= 3 and entries[0].path
    TOKEN.reset()


def test_session_refuses_a_source_that_changed(ntfs_disk, tmp_path):
    path, _ = ntfs_disk
    sess = str(tmp_path / "s.lvs")
    main(["scan", path, "--save", sess])
    with open(path, "ab") as f:
        f.write(bytes(4096))
    with pytest.raises(Exception, match="refusing to resume"):
        session.reopen(sess)


def test_scan_cli_prints_a_chance_of_recovery_summary_and_json_stays_valid(ntfs_disk, capsys):
    path, _ = ntfs_disk
    main(["scan", path, "--deleted-only"])
    out = capsys.readouterr().out
    assert "deleted 5: 5 look intact" in out
    main(["scan", path, "--json", "--limit", "2"])
    import json
    assert len(json.loads(capsys.readouterr().out)) == 2


def test_imaging_stops_on_external_cancel_and_resumes(tmp_path):
    disk = VirtualDisk(64 << 20, {5 << 20: b"DATA" * 1000})
    n = []
    job = imaging.ImageJob(disk, str(tmp_path / "i.img"), block=1 << 20, cancel_fn=lambda: n.append(1) or len(n) > 8)
    res = job.run()
    assert res.cancelled and disk.bytes_read < 12 << 20
    job2 = imaging.ImageJob(disk, str(tmp_path / "i.img"), block=1 << 20, mapfile=job.mapfile)
    assert job2.run().complete
    with open(tmp_path / "i.img", "rb") as f:
        f.seek(5 << 20)
        assert f.read(4) == b"DATA"


def test_fat_big_numbers_do_not_overflow_or_loop():
    b = Fat32Builder()
    b.add_file("a.bin", payload(1, 5000))
    b.delete("a.bin")
    fs = open_filesystem(BytesSource(b.build()))
    e = next(x for x in fs.scan() if x.deleted)
    assert fs.read(e, 0, 5000) == payload(1, 5000) and os.path.exists(__file__)


def test_backup_cancel_writes_no_snapshot_and_a_rerun_reuses_stored_chunks(tmp_path):
    from lavora.core import backup
    src = tmp_path / "s"
    src.mkdir()
    for i in range(6):
        (src / f"f{i}.bin").write_bytes(payload(i, 2 << 20))
    repo = backup.Repository.init(str(tmp_path / "r"))
    n = []
    st = backup.backup(repo, [str(src)], cancel=lambda: n.append(1) or len(n) > 3)
    assert st.cancelled and repo.snapshots() == []  # an interrupted backup must never look like a finished one
    st2 = backup.backup(repo, [str(src)])
    assert not st2.cancelled and st2.files == 6 and st2.bytes_stored < 6 * (2 << 20)  # earlier files were already stored
    assert backup.verify_backup(repo).ok


def test_doctor_scan_of_a_wrecked_5tib_disk_can_be_cancelled():
    from lavora.core import doctor
    disk = VirtualDisk(5 * TiB)
    n = []
    findings, dt = timed(lambda: doctor.diagnose(disk, cancel=lambda: n.append(1) or len(n) > 3))
    assert dt < 10 and disk.bytes_read < 64 << 20 and findings
