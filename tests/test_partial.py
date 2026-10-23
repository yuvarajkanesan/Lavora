"""Partial images: which files touch never-copied areas, recovery of the rest, and the 'Recover what's copied so far' button."""
import json

import pytest
from conftest import payload
from imgbuilders import ExfatBuilder, ExtBuilder, Fat32Builder, NtfsBuilder, make_mbr_disk

from lavora.cli.main import main
from lavora.core import unread
from lavora.core.device import BytesSource
from lavora.core.fs import open_filesystem


def assemble(fs, entry):
    """Rebuild a file purely from byte_ranges(): the ground-truth check that the ranges say where the data really is."""
    ranges = fs.byte_ranges(entry)
    return b"".join(fs.source.read_at(o, n) for o, n in ranges)[:entry.size]


def by_name(fs, **kw):
    return {e.name: e for e in fs.scan(**kw)}


def test_ntfs_byte_ranges_match_the_data_including_fragments_and_deleted():
    b = NtfsBuilder()
    frag, gone, tiny = payload(1, 90_000), payload(2, 33_000), b"tiny resident"
    b.add_file("frag.bin", frag, fragments=3)
    b.add_file("gone.bin", gone)
    b.add_file("tiny.txt", tiny)
    b.delete("gone.bin")
    fs = open_filesystem(BytesSource(b.build()))
    files = by_name(fs)
    assert len(fs.byte_ranges(files["frag.bin"])) == 3 and assemble(fs, files["frag.bin"]) == frag
    assert assemble(fs, files["gone.bin"]) == gone
    assert fs.byte_ranges(files["tiny.txt"]) == []  # resident: lives in the MFT record, not in clusters


def test_fat32_byte_ranges_live_and_deleted():
    b = Fat32Builder()
    a, c = payload(3, 20_000), payload(4, 7_000)
    b.add_file("A.BIN", a)
    b.add_file("C.BIN", c)
    b.delete("C.BIN")
    fs = open_filesystem(BytesSource(b.build()))
    files = by_name(fs)
    assert assemble(fs, files["A.BIN"]) == a and assemble(fs, next(e for e in files.values() if e.deleted)) == c


def test_exfat_byte_ranges_contiguous_and_chained():
    b = ExfatBuilder()
    x, y = payload(5, 30_000), payload(6, 12_000)
    b.add_file("x.bin", x)
    b.add_file("y.bin", y, nofat=False)
    fs = open_filesystem(BytesSource(b.build()))
    files = by_name(fs)
    assert assemble(fs, files["x.bin"]) == x and assemble(fs, files["y.bin"]) == y


@pytest.mark.parametrize("extents", [True, False])
def test_ext_byte_ranges(extents):
    b = ExtBuilder(extents=extents)
    d, e = payload(7, 15_000), payload(8, 6_000)
    b.add_file("d.bin", d)
    b.add_file("e.bin", e)
    b.delete("e.bin")
    fs = open_filesystem(BytesSource(b.build()))
    files = by_name(fs)
    assert assemble(fs, files["d.bin"]) == d and assemble(fs, files["e.bin"]) == e


def test_missing_percent_and_merge():
    assert unread.missing_percent([(100, 100)], [(150, 100)]) == 50.0
    assert unread.missing_percent([(100, 100), (1000, 100)], [(0, 50), (1050, 500)]) == 25.0
    assert unread.missing_percent([(100, 100)], [(0, 50)]) == 0.0
    assert unread.missing_percent([(100, 100)], [(0, 1000)], base=-0) == 100.0
    assert unread.missing_percent([(0, 100)], [(1000, 100)], base=1000) == 100.0  # volume offset is applied
    assert unread.merge([(10, 5), (15, 5), (30, 1), (0, 12)]) == [(0, 20), (30, 1)]


def test_load_unread_reads_the_imaging_map(tmp_path):
    img = tmp_path / "d.img"
    img.write_bytes(b"x")
    assert unread.load_unread(str(img)) is None  # no map: assume complete
    (tmp_path / "d.img.map").write_text(json.dumps({"size": 1000, "pass": 1, "next": 400, "suspect": [[100, 50]], "bad": [[120, 10]]}))
    assert unread.load_unread(str(img)) == [(100, 50), (400, 600)]  # skipped/bad blocks and everything after the copy position
    (tmp_path / "d.img.map").write_text(json.dumps({"size": 1000, "pass": 3, "next": 1000, "suspect": [], "bad": [[512, 512]]}))
    assert unread.load_unread(str(img)) == [(512, 512)]


@pytest.fixture
def partial_disk(tmp_path):
    """An NTFS volume with two deleted files; the 'second half' of the image was never copied (map says so)."""
    b = NtfsBuilder()
    first, second = payload(20, 30_000), payload(21, 30_000)
    b.add_file("first.bin", first)
    b.add_file("second.bin", second)
    b.delete("first.bin")
    b.delete("second.bin")
    disk, layout = make_mbr_disk([(b.build(), 7)])
    path = tmp_path / "partial.img"
    path.write_bytes(disk)
    fs = open_filesystem(BytesSource(disk[layout[0][0]:layout[0][0] + layout[0][1]]))
    files = by_name(fs)
    second_ranges = [(o + layout[0][0], n) for o, n in fs.byte_ranges(files["second.bin"])]
    (tmp_path / "partial.img.map").write_text(json.dumps({"size": len(disk), "pass": 1, "next": len(disk), "suspect": [list(r) for r in second_ranges], "bad": []}))
    return str(path), first, second


def test_files_touching_unread_areas_are_recovered_separately_and_reported(partial_disk, tmp_path, capsys):
    path, first, second = partial_disk
    out = tmp_path / "out"
    assert main(["recover", path, "--out", str(out), "--all", "--deleted-only"]) == 0
    text = capsys.readouterr().out
    assert "image is incomplete" in text and "1 incomplete (partly unread)" in text
    assert (out / "recovered" / "first.bin").read_bytes() == first  # fully inside the copied part: normal
    assert (out / "incomplete (partly unread)" / "second.bin").exists()  # flagged, kept apart so nobody mistakes it for good


def test_complete_images_are_unaffected(partial_disk, tmp_path):
    path, first, _ = partial_disk
    import os
    os.remove(path + ".map")
    out = tmp_path / "out2"
    assert main(["recover", path, "--out", str(out), "--all", "--deleted-only"]) == 0
    assert (out / "recovered" / "second.bin").exists() and not (out / "incomplete (partly unread)").exists()


# ------------------------------------------------------------------ GUI
def _app(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from lavora.gui.app import build_window
    app = QApplication.instance() or QApplication([])
    return build_window(app)


def pump(ms):
    from PySide6.QtCore import QEventLoop, QTimer
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def test_chance_column_shows_partly_unread(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from lavora.core.fs.base import FileEntry
    from lavora.gui.recover_page import chance_for
    ok = FileEntry("a", "a", 10, deleted=True)
    assert chance_for(ok)[0] == "Deleted" and chance_for(ok)[1][2] == "high"
    half = FileEntry("b", "b", 10, deleted=True, extra={"unread_pct": 50.0})
    state, (pct, label, level) = chance_for(half)
    assert state == "Partly unread" and pct < 60 and level == "medium"
    assert chance_for(FileEntry("c", "c", 10, deleted=True, extra={"unread_pct": 100.0}))[0] == "Unread"
    present = FileEntry("d", "d", 10, extra={"unread_pct": 30.0})
    assert chance_for(present)[0] == "Partly unread"


def test_partial_recover_button_without_an_image_explains_what_to_do(monkeypatch, tmp_path):
    win = _app(monkeypatch)
    from PySide6.QtWidgets import QMessageBox
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    page = win.pages["image"]
    page.src.edit.setText(r"\\.\PhysicalDrive2")
    page.dest.setText(str(tmp_path / "nothing-here"))
    page.partial_recover()
    assert shown and "no image yet" in shown[0].lower()


def test_partial_recover_button_opens_the_recover_page_and_scans_the_copied_part(monkeypatch, tmp_path, partial_disk):
    win = _app(monkeypatch)
    path, _first, _second = partial_disk
    import os
    page = win.pages["image"]
    folder = os.path.dirname(path)
    page._image_path = path
    page.src.edit.setText(path)  # derives to <folder>/lavora-partial.img only for devices; use the explicit file path instead
    page.dest.setText(path)  # a path ending in .img is used as-is
    page.partial_recover()
    rec = win.pages["recover"]
    assert win.stack.currentWidget() is rec and rec.src.path == path
    for _ in range(50):
        pump(100)
        if rec.entries:
            break
    assert any(e.name == "first.bin" for e in rec.entries)
    assert rec.unread  # the page knows this image is incomplete
    second = next(e for e in rec.entries if e.name == "second.bin")
    rec._annotate([second])
    assert second.extra.get("unread_pct", 0) > 0
    assert folder  # (kept for clarity)


def test_partial_recover_button_stops_a_running_imaging_job_first(monkeypatch, tmp_path, partial_disk):
    win = _app(monkeypatch)
    path, _a, _b = partial_disk
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    page = win.pages["image"]
    page.src.edit.setText(path)
    page.dest.setText(path)
    page._running = True  # pretend imaging is running
    page.partial_recover()
    assert page._goto_recover and page.token.cancelled  # it asked the job to stop; the recover page opens when the job reports back
