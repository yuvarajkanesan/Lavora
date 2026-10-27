"""Automatic recovery: every way a disk can be damaged must still end with the files recovered, via the next method in the chain."""
import hashlib
import os
import random
import time

from conftest import payload
from imgbuilders import ExfatBuilder, Fat32Builder, NtfsBuilder, make_mbr_disk

from lavora.cli.main import main
from lavora.core import autorecover
from lavora.core.device import BytesSource
from lavora.core.license import License


def jpeg(n: int, seed: int) -> bytes:
    body = random.Random(seed).randbytes(n).replace(b"\xff", b"\xfe")
    return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + body + b"\xff\xd9"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


FILES = {
    "Docs/report.docx": payload(1, 40_000),
    "Docs/notes.txt": b"some plain notes\n" * 200,
    "Photos/holiday.jpg": jpeg(30_000, 2),
    "Photos/old/lost.jpg": jpeg(22_000, 3),
    "backup.bin": payload(4, 15_000),
}
DELETED = ("Photos/old/lost.jpg", "backup.bin")


def build_ntfs():
    b = NtfsBuilder()
    for path, data in FILES.items():
        b.add_file(path, data)
    for path in DELETED:
        b.delete(path)
    return b


def recovered_hashes(out_dir):
    found = {}
    for root, _d, files in os.walk(out_dir):
        for f in files:
            if f.endswith(".html"):
                continue
            with open(os.path.join(root, f), "rb") as fh:
                found[sha(fh.read())] = os.path.join(root, f)
    return found


def run(disk: bytes, tmp_path, **kw):
    out = str(tmp_path / "out")
    rep = autorecover.auto_recover(BytesSource(disk, "<test>"), out, **kw)
    return rep, out


def assert_all_recovered(out, expected=FILES):
    got = recovered_hashes(out)
    missing = [p for p, d in expected.items() if sha(d) not in got]
    assert not missing, f"not recovered: {missing}"


def status(rep, stage_prefix):
    return [(s.stage, s.status) for s in rep.steps if s.stage.startswith(stage_prefix)]


def mbr_disk(vol: bytes, t=0x07):
    return make_mbr_disk([(vol, t)])


# ------------------------------------------------------------------------------------------------ the happy path
def test_intact_disk_recovers_existing_and_deleted_files(tmp_path):
    disk, _ = mbr_disk(build_ntfs().build())
    rep, out = run(disk, tmp_path)
    assert rep.recovered == len(FILES) and rep.failed == 0 and rep.carved == 0
    assert_all_recovered(out)
    assert rep.volumes[0].method == "primary metadata" and rep.volumes[0].found_by == "MBR partition table"
    assert os.path.isfile(rep.report_path) and "Lavora automatic recovery" in open(rep.report_path, encoding="utf-8").read()
    assert os.path.isdir(os.path.join(out, "Volume 1 (ntfs, 10.5 MB)", "recovered"))  # deleted files kept apart from existing ones


def test_deleted_only_option(tmp_path):
    disk, _ = mbr_disk(build_ntfs().build())
    rep, out = run(disk, tmp_path, include_live=False)
    assert rep.recovered == len(DELETED)
    got = recovered_hashes(out)
    assert sha(FILES["Docs/notes.txt"]) not in got and sha(FILES["backup.bin"]) in got


# ------------------------------------------------------------------------------------------------ fallback 1: backup metadata
def test_damaged_ntfs_boot_sector_falls_back_to_the_backup_boot_sector(tmp_path):
    disk, layout = mbr_disk(build_ntfs().build())
    d = bytearray(disk)
    d[layout[0][0]:layout[0][0] + 512] = bytes(512)
    rep, out = run(bytes(d), tmp_path)
    assert_all_recovered(out)
    assert rep.volumes[0].method == "NTFS backup boot sector (end of volume)"
    steps = status(rep, "Open volume")
    assert steps[0] == ("Open volume: primary metadata", "failed") and steps[1][1] == "ok"  # it tried the primary, failed, moved on


def test_damaged_mft_record_zero_falls_back_to_the_mft_mirror(tmp_path):
    b = build_ntfs()
    disk, layout = mbr_disk(b.build())
    d = bytearray(disk)
    rec0 = layout[0][0] + b.mft_lcn * b.cs
    d[rec0:rec0 + b.REC] = bytes(b.REC)  # $MFT's own record destroyed (the mirror at cluster 2 still has it)
    rep, out = run(bytes(d), tmp_path)
    assert rep.volumes[0].method.endswith("$MFT mirror ($MFTMirr)") or "mirror" in rep.volumes[0].method
    assert_all_recovered(out)


def test_both_boot_sector_and_mft_record_damaged_uses_the_combination(tmp_path):
    b = build_ntfs()
    disk, layout = mbr_disk(b.build())
    d = bytearray(disk)
    start = layout[0][0]
    d[start:start + 512] = bytes(512)
    d[start + b.mft_lcn * b.cs:start + b.mft_lcn * b.cs + b.REC] = bytes(b.REC)
    rep, out = run(bytes(d), tmp_path)
    assert "backup boot sector" in rep.volumes[0].method and "mirror" in rep.volumes[0].method
    assert_all_recovered(out)


def test_damaged_fat32_boot_sector_uses_the_backup_in_sector_6(tmp_path):
    b = Fat32Builder()
    data = {"A.TXT": b"hello fat", "photos/Holiday Picture.jpg": jpeg(9000, 5), "big.bin": payload(6, 30_000)}
    for p, d in data.items():
        b.add_file(p, d)
    b.delete("big.bin")
    disk, layout = mbr_disk(b.build(), 0x0C)
    d = bytearray(disk)
    d[layout[0][0]:layout[0][0] + 512] = bytes(512)
    rep, out = run(bytes(d), tmp_path)
    assert rep.volumes[0].method == "FAT32 backup boot sector (sector 6)"
    assert_all_recovered(out, data)


def test_damaged_exfat_boot_sector_uses_the_backup_region(tmp_path):
    b = ExfatBuilder()
    data = {"keep.bin": payload(7, 20_000), "pics/Vacation 2024.png": payload(8, 9_000)}
    for p, d in data.items():
        b.add_file(p, d)
    b.delete("pics/Vacation 2024.png")
    disk, layout = mbr_disk(b.build(), 0x07)
    d = bytearray(disk)
    d[layout[0][0]:layout[0][0] + 512] = bytes(512)
    rep, out = run(bytes(d), tmp_path)
    assert rep.volumes[0].method == "exFAT backup boot region (sector 12)"
    assert_all_recovered(out, data)


# ------------------------------------------------------------------------------------------------ fallback 2: find the volume
def test_wiped_partition_table_falls_back_to_the_lost_volume_scan(tmp_path):
    disk, _ = mbr_disk(build_ntfs().build())
    rep, out = run(bytes(512) + disk[512:], tmp_path)
    assert_all_recovered(out)
    assert rep.volumes[0].found_by.startswith("lost-volume scan")
    assert ("Find volumes: partition table", "failed") in status(rep, "Find volumes")


def test_wiped_table_and_damaged_boot_sector_still_recovers(tmp_path):
    disk, layout = mbr_disk(build_ntfs().build())
    d = bytearray(disk)
    d[:512] = bytes(512)
    d[layout[0][0]:layout[0][0] + 512] = bytes(512)  # table gone AND primary boot sector gone: found through the backup boot sector
    rep, out = run(bytes(d), tmp_path)
    assert_all_recovered(out)
    assert "backup" in rep.volumes[0].method


def test_filesystem_without_any_partition_table(tmp_path):
    rep, out = run(build_ntfs().build(), tmp_path)  # a bare volume image
    assert_all_recovered(out)
    assert rep.volumes[0].found_by.startswith("filesystem at the start")


# ------------------------------------------------------------------------------------------------ fallback 3: carving
def test_unrecoverable_filesystem_falls_back_to_carving_by_content(tmp_path):
    b = build_ntfs()
    disk, layout = mbr_disk(b.build())
    d = bytearray(disk)
    start, size = layout[0]
    d[start:start + 512] = bytes(512)  # primary boot sector
    d[start + size - 512:start + size] = bytes(512)  # backup boot sector
    d[start + b.mft_lcn * b.cs:start + b.mft_lcn * b.cs + 8 * b.REC] = bytes(8 * b.REC)  # first MFT records AND their mirror's source
    d[start + 2 * b.cs:start + 2 * b.cs + 4 * b.REC] = bytes(4 * b.REC)  # the $MFTMirr copy
    rep, out = run(bytes(d), tmp_path)
    assert rep.volumes[0].error and rep.carved >= 2  # no filesystem usable: the JPEGs come back by content
    got = recovered_hashes(out)
    assert sha(FILES["Photos/holiday.jpg"]) in got and sha(FILES["Photos/old/lost.jpg"]) in got
    assert os.path.isdir(os.path.join(out, autorecover.CARVED_DIR, "jpg"))
    assert any(s.status == "failed" for s in rep.steps) and any("Carve" in s.stage for s in rep.steps)


def test_thorough_mode_adds_carving_without_duplicating_what_the_filesystem_gave(tmp_path):
    disk, _ = mbr_disk(build_ntfs().build())
    rep, out = run(disk, tmp_path, thorough=True)
    assert rep.recovered == len(FILES) and rep.carved == 0 and rep.duplicates_skipped >= 2  # JPEGs seen twice, kept once
    assert_all_recovered(out)


def test_thorough_mode_finds_a_volume_missing_from_the_partition_table(tmp_path):
    a, b = build_ntfs(), Fat32Builder()
    b.add_file("X.TXT", b"second volume file")
    disk, layout = make_mbr_disk([(a.build(), 0x07), (b.build(), 0x0C)])
    d = bytearray(disk)
    d[446 + 16:446 + 32] = bytes(16)  # second partition entry deleted from the table
    rep, out = run(bytes(d), tmp_path, thorough=True)
    assert len(rep.volumes) == 2 and rep.volumes[1].found_by.startswith("lost-volume scan")
    assert sha(b"second volume file") in recovered_hashes(out)


# ------------------------------------------------------------------------------------------------ robustness
def test_free_tier_limit_is_reported_not_fatal(tmp_path):
    class Tiny(License):
        def recovery_limit(self):
            return 1000
    disk, _ = mbr_disk(build_ntfs().build())
    rep, _out = run(disk, tmp_path, license=Tiny())
    assert rep.recovered == 0 and rep.volumes[0].error and any("Pro" in s.detail for s in rep.steps)


def test_cancel_stops_promptly_and_still_writes_a_report(tmp_path):
    disk, _ = mbr_disk(build_ntfs().build())
    t = time.perf_counter()
    rep, _out = run(disk, tmp_path, cancel=lambda: True)
    assert rep.cancelled and time.perf_counter() - t < 5 and os.path.isfile(rep.report_path)


def test_incomplete_files_from_a_partial_image_are_kept_apart(tmp_path):
    b = build_ntfs()
    disk, layout = mbr_disk(b.build())
    from lavora.core.fs import open_filesystem
    fs = open_filesystem(BytesSource(disk[layout[0][0]:layout[0][0] + layout[0][1]]))
    f = next(e for e in fs.scan() if e.name == "report.docx")
    unread = [(o + layout[0][0], n) for o, n in fs.byte_ranges(f)]
    rep, out = run(disk, tmp_path, unread=unread)
    assert rep.incomplete == 1 and rep.unread_bytes > 0
    assert any("incomplete (partly unread)" in root for root, _d, files in os.walk(out) if "report.docx" in files)


def test_a_volume_that_explodes_does_not_stop_the_others(tmp_path, monkeypatch):
    a, c = build_ntfs(), Fat32Builder()
    c.add_file("Z.TXT", b"survivor")
    disk, _ = make_mbr_disk([(a.build(), 0x07), (c.build(), 0x0C)])
    real = autorecover.recover
    calls = {"n": 0}

    def flaky(fs, *args, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated parser bug")
        return real(fs, *args, **kw)
    monkeypatch.setattr(autorecover, "recover", flaky)
    rep, out = run(disk, tmp_path)
    assert rep.volumes[0].error and sha(b"survivor") in recovered_hashes(out)


# ------------------------------------------------------------------------------------------------ CLI
def test_cli_autorecover_end_to_end(tmp_path, capsys):
    disk, layout = mbr_disk(build_ntfs().build())
    d = bytearray(disk)
    d[layout[0][0]:layout[0][0] + 512] = bytes(512)  # damaged boot sector, so the log shows a fallback
    img = tmp_path / "broken.img"
    img.write_bytes(bytes(d))
    out = tmp_path / "rec"
    assert main(["autorecover", str(img), "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "[FAIL] Open volume: primary metadata" in text and "[ok  ] Open volume: NTFS backup boot sector" in text
    assert f"{len(FILES)} file(s) recovered" in text and "report:" in text
    assert_all_recovered(str(out))


def test_cli_autorecover_returns_1_when_nothing_can_be_found(tmp_path, capsys):
    img = tmp_path / "blank.img"
    img.write_bytes(bytes(2 << 20))
    assert main(["autorecover", str(img), "--out", str(tmp_path / "o")]) == 1
    assert "0 file(s) recovered" in capsys.readouterr().out



# ------------------------------------------------------------------------------------------------ GUI
def test_gui_recover_everything_page_runs_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    from lavora.gui.app import build_window
    app = QApplication.instance() or QApplication([])
    win = build_window(app)
    disk, layout = mbr_disk(build_ntfs().build())
    d = bytearray(disk)
    d[layout[0][0]:layout[0][0] + 512] = bytes(512)
    img = tmp_path / "gui.img"
    img.write_bytes(bytes(d))
    page = win.pages["auto"]
    win.show_page("auto")
    page.src.edit.setText(str(img))
    page.dest.setText(str(tmp_path / "gui-out"))
    page.start()
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(100)
    timer.timeout.connect(lambda: loop.quit() if page.open_btn.isEnabled() else None)
    timer.start()
    QTimer.singleShot(20_000, loop.quit)
    loop.exec()
    assert page.open_btn.isEnabled() and f"{len(FILES)} file(s) recovered" in page.summary.text()
    log = page.log.toPlainText()
    assert "FAIL" in log and "NTFS backup boot sector" in log  # the log shows the failed method and the fallback that worked
    assert_all_recovered(str(tmp_path / "gui-out"))


def test_gui_asks_for_the_missing_fields(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox

    from lavora.gui.app import build_window
    app = QApplication.instance() or QApplication([])
    win = build_window(app)
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    page = win.pages["auto"]
    page.start()
    page.src.edit.setText("x.img")
    page.start()
    assert len(shown) == 2 and "disk or image" in shown[0] and "different drive" in shown[1]
