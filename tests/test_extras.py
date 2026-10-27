"""Newer options: more carved types, 'recover only photos/documents', disk health check."""
import hashlib
import os
import random
import struct
import time

import pytest
from conftest import payload
from imgbuilders import NtfsBuilder, make_mbr_disk

from lavora.cli.main import main
from lavora.core import autorecover, carver, health
from lavora.core.device import BlockSource, BytesSource


def sha(b):
    return hashlib.sha256(b).hexdigest()


def riff(kind: bytes, n: int, seed: int) -> bytes:
    body = kind + random.Random(seed).randbytes(n)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def noise_with(items, size=1 << 20, seed=1):
    rnd = random.Random(seed)
    disk = bytearray(rnd.randbytes(size))
    for sig in (b"RIFF", b"\xff\xd8\xff", b"ftyp", b"PK\x03\x04", b"%PDF-", b"ID3", b"BM", b"GIF8"):
        i = disk.find(sig)
        while i != -1:
            disk[i] = 1
            i = disk.find(sig, i + 1)
    for off, data in items:
        disk[off:off + len(data)] = data
    return bytes(disk)


# ------------------------------------------------------------------ carver
def test_riff_family_is_told_apart_and_sized_exactly():
    wav, avi, webp = riff(b"WAVE", 3000, 1), riff(b"AVI ", 5000, 2), riff(b"WEBP", 2000, 3)
    disk = noise_with([(4096, wav), (40_000, avi), (90_000, webp)])
    found = {c.offset: c for c in carver.carve(BytesSource(disk))}
    assert (found[4096].sig.ext, found[4096].size) == ("wav", len(wav))
    assert (found[40_000].sig.ext, found[40_000].size) == ("avi", len(avi))
    assert (found[90_000].sig.ext, found[90_000].size) == ("webp", len(webp))
    assert sha(disk[4096:4096 + found[4096].size]) == sha(wav)


@pytest.mark.parametrize("brand,ext", [(b"heic", "heic"), (b"qt  ", "mov"), (b"3gp5", "3gp"), (b"M4A ", "m4a"), (b"isom", "mp4")])
def test_iso_bmff_files_are_named_by_their_brand(brand, ext):
    head = struct.pack(">I4s", 24, b"ftyp") + brand + bytes(4) + brand + b"mp42"
    assert carver.brand_ext(head) == ext


# ------------------------------------------------------------------ autorecover --only
def test_parse_only_presets_and_extensions():
    assert autorecover.parse_only(None) is None and autorecover.parse_only("all") is None and autorecover.parse_only("") is None
    s = autorecover.parse_only("photos,.PDF")
    assert {"jpg", "png", "heic", "pdf"} <= s and "mp4" not in s


def _jpeg(n, seed):
    return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + random.Random(seed).randbytes(n).replace(b"\xff", b"\xfe") + b"\xff\xd9"


def test_only_photos_skips_other_files_in_the_filesystem_and_in_carving(tmp_path):
    b = NtfsBuilder()
    files = {"a/pic.jpg": _jpeg(9000, 1), "a/doc.pdf": b"%PDF-1.4\n" + b"x" * 3000 + b"\n%%EOF\n", "a/data.bin": payload(2, 8000)}
    for p, d in files.items():
        b.add_file(p, d)
    disk, _ = make_mbr_disk([(b.build(), 7)])
    out = str(tmp_path / "o")
    rep = autorecover.auto_recover(BytesSource(disk), out, only_exts=autorecover.parse_only("photos"))
    names = {f for _r, _d, fs in os.walk(out) for f in fs if not f.endswith(".html")}
    assert names == {"pic.jpg"} and rep.recovered == 1

    # carving honours the filter too: wipe the filesystem, ask for photos only
    d = bytearray(disk)
    d[1 << 20:(1 << 20) + 512] = bytes(512)
    d[len(d) - 512:] = bytes(512)
    d[(1 << 20) + b.mft_lcn * b.cs:(1 << 20) + b.mft_lcn * b.cs + 8 * b.REC] = bytes(8 * b.REC)
    d[(1 << 20) + 2 * b.cs:(1 << 20) + 2 * b.cs + 4 * b.REC] = bytes(4 * b.REC)
    out2 = str(tmp_path / "o2")
    rep2 = autorecover.auto_recover(BytesSource(bytes(d)), out2, only_exts=autorecover.parse_only("photos"))
    kinds = {os.path.splitext(f)[1] for _r, _d, fs in os.walk(out2) for f in fs if not f.endswith(".html")}
    assert rep2.carved >= 1 and kinds == {".jpg"}


def test_cli_only_option(tmp_path, capsys):
    b = NtfsBuilder()
    b.add_file("pic.jpg", _jpeg(5000, 3))
    b.add_file("notes.txt", b"hello notes")
    disk, _ = make_mbr_disk([(b.build(), 7)])
    img = tmp_path / "d.img"
    img.write_bytes(disk)
    out = tmp_path / "rec"
    assert main(["autorecover", str(img), "--out", str(out), "--only", "documents"]) == 0
    names = {f for _r, _d, fs in os.walk(out) for f in fs if not f.endswith(".html")}
    assert names == {"notes.txt"}
    capsys.readouterr()


# ------------------------------------------------------------------ health
class Zone(BlockSource):
    def __init__(self, size, bad=None, slow=None, delay=0.0):
        self.size, self.path, self.bad, self.slow, self.delay = size, "<z>", bad, slow, delay

    def read_at(self, offset, length):
        if self.bad and offset < self.bad[1] and offset + length > self.bad[0]:
            raise OSError(5, "Input/output error")
        if self.slow and offset < self.slow[1] and offset + length > self.slow[0]:
            time.sleep(self.delay)
        return bytes(min(length, self.size - offset))


def test_health_verdicts():
    ok = health.check_health(Zone(16 << 20))
    assert ok.verdict == "healthy" and ok.scanned == 16 << 20 and ok.speed > 0 and "No unreadable" in ok.advice()
    slow = health.check_health(Zone(16 << 20, slow=(4 << 20, 6 << 20), delay=0.05), slow_after=0.02)
    assert slow.verdict == "slow" and len(slow.slow) == 2
    bad = health.check_health(Zone(16 << 20, bad=(8 << 20, 10 << 20)))
    assert bad.verdict == "failing" and len(bad.unreadable) == 2 and "image it now" in bad.advice()
    assert "verdict: failing" in bad.summary_text()


def test_health_limit_and_cancel():
    part = health.check_health(Zone(64 << 20), max_bytes=8 << 20)
    assert part.scanned == 8 << 20 and part.total == 8 << 20
    n = []
    stopped = health.check_health(Zone(64 << 20), cancel=lambda: n.append(1) or len(n) > 3)
    assert stopped.cancelled and stopped.scanned < 64 << 20


def test_cli_health_exit_codes(tmp_path, capsys):
    img = tmp_path / "h.img"
    img.write_bytes(payload(1, 3 << 20))
    assert main(["health", str(img), "--gb", "0.002"]) == 0
    assert "verdict: healthy" in capsys.readouterr().out


def test_gui_health_page_runs(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    from lavora.gui.app import build_window
    app = QApplication.instance() or QApplication([])
    win = build_window(app)
    img = tmp_path / "g.img"
    img.write_bytes(payload(2, 4 << 20))
    page = win.pages["health"]
    win.show_page("health")
    page.src.edit.setText(str(img))
    page.start()
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(100)
    timer.timeout.connect(lambda: loop.quit() if page._report else None)
    timer.start()
    QTimer.singleShot(15_000, loop.quit)
    loop.exec()
    assert page._report and page.verdict.text() == "Healthy" and page.stats["bad"].text() == "0"
    assert not page.image_btn.isVisibleTo(page)
