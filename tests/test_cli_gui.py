"""End-to-end: the `lavora` CLI on generated disk images, plus a headless GUI smoke test."""
import json
import os

import pytest
from conftest import payload, sha
from imgbuilders import Fat32Builder, NtfsBuilder, make_mbr_disk

from lavora.cli.main import main
from lavora.core import license


@pytest.fixture
def pro(monkeypatch):
    monkeypatch.setattr(license, "current", lambda: license.License("pro", valid=True))


@pytest.fixture
def disk_path(tmp_path):
    ntfs = NtfsBuilder()
    ntfs.add_file("keep.txt", b"keep")
    ntfs.add_file("Photos/holiday.jpg", payload(5, 25_000))
    ntfs.add_file("Photos/other.jpg", payload(6, 3_000))
    ntfs.delete("Photos/holiday.jpg")
    disk, _ = make_mbr_disk([(ntfs.build(), 0x07)])
    p = tmp_path / "disk.img"
    p.write_bytes(disk)
    return str(p)


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_version_and_help(capsys):
    code, out, _ = run(capsys, "version")
    assert code == 0 and "Lavora" in out and "get your system working again" in out


def test_partitions_and_scan_json(capsys, disk_path):
    code, out, _ = run(capsys, "partitions", disk_path, "--json")
    assert code == 0 and json.loads(out)["partitions"][0]["fs"] == "ntfs"
    code, out, _ = run(capsys, "scan", disk_path, "--deleted-only", "--json")
    rows = json.loads(out)
    assert code == 0 and [r["path"] for r in rows] == ["Photos/holiday.jpg"] and rows[0]["deleted"]


def test_recover_end_to_end_with_report(capsys, disk_path, tmp_path):
    out_dir, rep = tmp_path / "rec", tmp_path / "report.html"
    code, out, _ = run(capsys, "recover", disk_path, "--out", str(out_dir), "--deleted-only", "--all", "--report", str(rep))
    assert code == 0 and "1 recovered" in out
    f = next(out_dir.rglob("holiday.jpg"))
    assert sha(f.read_bytes()) == sha(payload(5, 25_000))
    assert "holiday.jpg" in rep.read_text()


def test_recover_dry_run_and_nothing_selected(capsys, disk_path, tmp_path):
    code, out, _ = run(capsys, "recover", disk_path, "--out", str(tmp_path / "o"), "--all", "--dry-run")
    assert code == 0 and "would write" in out and not (tmp_path / "o").exists()
    code, _o, err = run(capsys, "recover", disk_path, "--out", str(tmp_path / "o"))
    assert code == 2 and "nothing selected" in err


def test_recover_refuses_to_write_the_image_itself(capsys, disk_path):
    code, _o, err = run(capsys, "recover", disk_path, "--out", disk_path, "--all")
    assert code == 2 and ("itself" in err or "file" in err)


def test_free_tier_blocks_pro_features(capsys, disk_path, tmp_path):
    for argv in (("image", disk_path, str(tmp_path / "x.img")), ("doctor", disk_path),
                 ("backup", str(tmp_path / "b"), str(tmp_path))):
        code, _o, err = run(capsys, *argv)
        assert code == 2 and "Pro feature" in err, argv


def test_image_clone_and_verify_pro(capsys, pro, disk_path, tmp_path):
    img = tmp_path / "copy.img"
    code, out, _ = run(capsys, "image", disk_path, str(img))
    assert code == 0 and "complete=True" in out and img.read_bytes() == open(disk_path, "rb").read()
    code, out, _ = run(capsys, "clone", disk_path, str(tmp_path / "clone.img"), "--verify")
    assert code == 0 and "verify=ok" in out


def test_doctor_repairs_and_undo_via_cli(capsys, pro, disk_path, tmp_path):
    original = open(disk_path, "rb").read()
    with open(disk_path, "r+b") as f:
        f.write(bytes(512))
    code, out, _ = run(capsys, "doctor", disk_path)
    assert "No valid partition table" in out and "rebuild-mbr" in out
    code, out, _ = run(capsys, "doctor", disk_path, "--fix", "rebuild-mbr")
    assert "dry run" in out
    code, out, _ = run(capsys, "doctor", disk_path, "--fix", "rebuild-mbr", "--apply")
    assert "APPLIED" in out
    undo_id = out.split("lavora undo ")[1].split()[0]
    code, out, _ = run(capsys, "doctor", disk_path)
    assert "No problems found" in out
    run(capsys, "undo", undo_id)
    assert open(disk_path, "rb").read()[:512] == bytes(512) and open(disk_path, "rb").read()[512:] == original[512:]


def test_basic_repair_is_free(capsys, disk_path):
    with open(disk_path, "r+b") as f:
        f.write(bytes(512))
    code, out, _ = run(capsys, "repair", disk_path, "--fix", "rebuild-mbr", "--apply")
    assert code == 0 and "APPLIED" in out


def test_carve_cli(capsys, tmp_path):
    jpg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x01" * 300 + b"\xff\xd9"
    blob = bytes(10_000) + jpg + bytes(10_000)
    p = tmp_path / "raw.bin"
    p.write_bytes(blob)
    code, out, _ = run(capsys, "carve", str(p), "--out", str(tmp_path / "carved"))
    assert code == 0 and "1 file(s) carved" in out
    assert next((tmp_path / "carved").iterdir()).read_bytes() == jpg


def test_backup_cli_with_verification_and_restore(capsys, pro, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.txt").write_text("hello" * 100)
    code, out, _ = run(capsys, "backup", str(tmp_path / "repo"), str(src), "--verify")
    assert code == 0 and "verification (test restore): OK" in out
    code, out, _ = run(capsys, "restore", str(tmp_path / "repo"), "--out", str(tmp_path / "back"))
    assert code == 0 and (tmp_path / "back" / "src" / "x.txt").read_text() == "hello" * 100
    code, out, _ = run(capsys, "verify-backup", str(tmp_path / "repo"))
    assert code == 0 and "OK" in out


def test_plan_and_account_cli(capsys, lavora_home):
    code, out, _ = run(capsys, "plan", "grub", "--root", "/dev/sda2", "--disk", "/dev/sda", "--efi", "/dev/sda1")
    assert code == 0 and "grub-install --target=x86_64-efi" in out
    code, _o, err = run(capsys, "plan", "windows-bcd", "--execute")
    assert code == 2 and "RUN REPAIR PLAN" in err
    code, _o, err = run(capsys, "account", "windows", "D:\\Windows", "bob")
    assert code == 2 and "this is my device" in err
    code, out, _ = run(capsys, "account", "windows", "D:\\Windows", "bob", "--attest", "this is my device")
    assert code == 2 and "not implemented" in out.lower() or "not available" in out.lower()
    assert os.path.exists(lavora_home / "audit.log")


def test_license_cli(capsys, lavora_home):
    code, out, _ = run(capsys, "license")
    assert "tier: free" in out
    code, _o, err = run(capsys, "license", "install", "LAVORA-AAAA.BBBB")
    assert code == 2 and "rejected" in err


def test_fat_image_cli(capsys, tmp_path):
    b = Fat32Builder()
    b.add_file("a.txt", b"hello")
    p = tmp_path / "f.img"
    p.write_bytes(b.build())
    code, out, _ = run(capsys, "scan", str(p), "--json")
    assert code == 0 and json.loads(out)[0]["path"] == "a.txt"


def test_gui_builds_and_pages_switch(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from lavora.gui.app import build_window
    app = QApplication.instance() or QApplication([])
    win = build_window(app)
    for key in win.pages:
        win.show_page(key)
        assert win.stack.currentWidget() is win.pages[key]
    boot = win.pages["boot"]
    boot.kind.setCurrentIndex(4)
    assert "grub-install" in boot.out.toPlainText()


def test_gui_recover_flow_scans_an_image(monkeypatch, tmp_path, disk_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    from lavora.gui.app import build_window
    app = QApplication.instance() or QApplication([])
    win = build_window(app)
    page = win.pages["recover"]
    page.src.edit.setText(disk_path)
    page.scan()
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(50)
    timer.timeout.connect(lambda: loop.quit() if page.entries else None)
    timer.start()
    QTimer.singleShot(10_000, loop.quit)
    loop.exec()
    assert any(e.deleted and e.name == "holiday.jpg" for e in page.entries)
    assert page.table.rowCount() == len(page.shown)
    page.filter.setText("holiday")
    page.apply_filter()  # the real UI debounces typing by 300 ms
    assert page.table.rowCount() == 1
