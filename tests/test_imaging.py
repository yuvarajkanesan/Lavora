"""Imaging: bad-sector map, multi-pass retry, pause/resume/cancel, compression, cloning, verification."""
import gzip
import threading

import pytest
from conftest import payload, sha

from lavora.core import imaging, smart
from lavora.core.device import SECTOR, BlockSource, BytesSource, ImageSource
from lavora.core.safety import SafetyError


class FaultySource(BlockSource):
    """Wraps data; reads touching a bad range raise OSError. `heal` makes ranges readable again after N attempts."""

    def __init__(self, data: bytes, bad: list[tuple[int, int]], heal_after: dict[int, int] | None = None):
        self._data, self.bad, self.size, self.path = data, bad, len(data), "<faulty>"
        self.attempts: dict[int, int] = {}
        self.heal_after = heal_after or {}

    def read_at(self, offset: int, length: int) -> bytes:
        for bo, bl in self.bad:
            if offset < bo + bl and offset + length > bo:
                key = bo
                self.attempts[key] = self.attempts.get(key, 0) + 1
                if self.attempts[key] <= self.heal_after.get(key, 10 ** 9):
                    raise OSError("simulated bad sector")
        return self._data[offset:offset + length]


@pytest.fixture
def disk():
    return payload(1, 4 * 1024 * 1024)


def test_clean_image_is_identical_and_hashed(tmp_path, disk):
    out = tmp_path / "a.img"
    res = imaging.ImageJob(BytesSource(disk), str(out), hash_result=True).run()
    assert res.complete and not res.bad and out.read_bytes() == disk
    assert res.sha256 == sha(disk)


def test_bad_sectors_are_mapped_and_zero_filled(tmp_path, disk):
    bad = [(1_000_448, SECTOR), (3_000_320, 2 * SECTOR)]
    out = tmp_path / "b.img"
    job = imaging.ImageJob(FaultySource(disk, bad), str(out), block=1 << 18)
    res = job.run()
    assert not res.complete and res.bad == bad
    img = out.read_bytes()
    assert len(img) == len(disk)
    for bo, bl in bad:
        assert img[bo:bo + bl] == bytes(bl)
    # everything outside the bad ranges was copied
    assert img[:bad[0][0]] == disk[:bad[0][0]] and img[-1000:] == disk[-1000:]
    assert imaging.merge_ranges([tuple(r) for r in __import__("json").load(open(job.mapfile))["bad"]]) == bad


def test_retry_passes_recover_flaky_sectors(tmp_path, disk):
    bad = [(512_000, SECTOR)]
    src = FaultySource(disk, bad, heal_after={512_000: 3})  # fails 3 times, then reads fine
    out = tmp_path / "c.img"
    res = imaging.ImageJob(src, str(out), block=1 << 18, passes=4).run()
    assert res.complete and out.read_bytes() == disk


def test_cancel_then_resume_finishes_without_rereading_everything(tmp_path, disk):
    out = tmp_path / "d.img"
    src = BytesSource(disk)
    job = imaging.ImageJob(src, str(out), block=1 << 16)
    seen = []

    def stop_halfway(done, total):
        seen.append(done)
        if done >= total // 2:
            job.cancel()

    r1 = job.run(stop_halfway)
    assert r1.cancelled and not r1.complete
    job2 = imaging.ImageJob(src, str(out), block=1 << 16, mapfile=job.mapfile)
    calls = []
    r2 = job2.run(lambda d, t: calls.append(d))
    assert r2.complete and out.read_bytes() == disk
    assert min(calls) > len(disk) // 2 - (1 << 17)  # resumed near the stop point, not from zero


def test_pause_blocks_until_resume(tmp_path, disk):
    out = tmp_path / "e.img"
    job = imaging.ImageJob(BytesSource(disk), str(out), block=1 << 16)
    job.pause()
    done = threading.Event()
    t = threading.Thread(target=lambda: (job.run(), done.set()))
    t.start()
    assert not done.wait(0.3)
    job.resume()
    assert done.wait(10)
    t.join()
    assert out.read_bytes() == disk


def test_compressed_image(tmp_path, disk):
    out = tmp_path / "f.img"
    res = imaging.ImageJob(BytesSource(disk), str(out), compress=True).run()
    assert res.path.endswith(".gz") and not out.exists()
    assert gzip.open(res.path).read() == disk


def test_refuses_to_overwrite_an_existing_image(tmp_path, disk):
    out = tmp_path / "g.img"
    out.write_bytes(b"precious")
    with pytest.raises(SafetyError):
        imaging.ImageJob(BytesSource(disk), str(out))


def test_clone_and_verify(tmp_path, disk):
    src_path = tmp_path / "src.img"
    src_path.write_bytes(disk)
    dst = tmp_path / "dst.img"
    with ImageSource(str(src_path)) as src:
        res = imaging.clone(src, str(dst))
        assert res.complete
        with ImageSource(str(dst)) as copy:
            assert imaging.verify_copy(src, copy)
    dst.write_bytes(disk[:100] + b"X" + disk[101:])
    with ImageSource(str(src_path)) as src, ImageSource(str(dst)) as copy:
        assert not imaging.verify_copy(src, copy)


def test_clone_refuses_same_target_and_requires_typed_confirmation_for_devices(tmp_path, disk):
    p = tmp_path / "s.img"
    p.write_bytes(disk)
    with ImageSource(str(p)) as src:
        with pytest.raises(SafetyError):
            imaging.clone(src, str(p))
        with pytest.raises(SafetyError, match="ERASE"):
            imaging.clone(src, r"\\.\PhysicalDrive9")
        with pytest.raises(SafetyError):
            imaging.clone(src, r"\\.\PhysicalDrive9", confirmation="yes")


def test_surface_scan_reports_bad_blocks(disk):
    res = smart.surface_scan(FaultySource(disk, [(2 << 20, 4096)]), block=1 << 18)
    assert not res.healthy and res.bad == [(2 << 20, 1 << 18)]
    assert smart.surface_scan(BytesSource(disk), block=1 << 18).healthy


def test_vhd_footer_is_not_part_of_the_disk(tmp_path):
    data = payload(5, 8192)
    footer = bytearray(512)
    footer[:8] = b"conectix"
    footer[60:64] = (2).to_bytes(4, "big")
    p = tmp_path / "d.vhd"
    p.write_bytes(data + bytes(footer))
    with ImageSource(str(p)) as s:
        assert s.size == 8192 and s.read_at(0, 8192) == data
