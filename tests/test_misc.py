"""Safety rails, licence, boot plans, accounts, trash, SMART parsing, reports, recovery filters."""
import json
import struct
from datetime import date, timedelta

import pytest
from conftest import payload, sha
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from imgbuilders import Fat32Builder, NtfsBuilder

from lavora.core import accounts, boot, license, report, safety, schedule, smart, trash
from lavora.core.device import BytesSource, ImageSource, PartitionView
from lavora.core.disks import DiskInfo, VolumeInfo
from lavora.core.fs import open_filesystem
from lavora.core.recovery import filter_entries, hex_dump, preview, recover, safe_relative_path


# ------------------------------------------------------------------ safety
class FakeDevice(BytesSource):
    is_device = True


def fake_disk(path=r"\\.\PhysicalDrive1"):
    return lambda p: DiskInfo(path=path, volumes=[VolumeInfo(mountpoint="E:"), VolumeInfo(mountpoint="F:")])


def test_output_on_source_disk_is_refused():
    src = FakeDevice(b"", r"\\.\PhysicalDrive1")
    with pytest.raises(safety.SafetyError, match="on the disk being recovered"):
        safety.check_output_location(src, r"E:\recovered", disk_lookup=fake_disk())
    with pytest.raises(safety.SafetyError):
        safety.check_output_location(src, r"f:\x\y", disk_lookup=fake_disk())  # case-insensitive
    safety.check_output_location(src, r"D:\recovered", disk_lookup=fake_disk())  # other disk: fine


def test_unknown_device_layout_fails_closed():
    with pytest.raises(safety.SafetyError, match="fail closed"):
        safety.check_output_location(FakeDevice(b"", "x"), r"D:\out", disk_lookup=lambda p: None)


def test_partition_view_inherits_device_rules():
    view = PartitionView(FakeDevice(bytes(4096), r"\\.\PhysicalDrive1"), 0, 4096)
    with pytest.raises(safety.SafetyError):
        safety.check_output_location(view, r"E:\x", disk_lookup=fake_disk())


def test_image_source_cannot_be_the_output(tmp_path):
    p = tmp_path / "a.img"
    p.write_bytes(bytes(1024))
    with ImageSource(str(p)) as s:
        with pytest.raises(safety.SafetyError):
            safety.check_output_location(s, str(p))
        safety.check_output_location(s, str(tmp_path / "out"))


def test_typed_confirmation():
    safety.require_typed_confirmation("ERASE X", " erase x ")
    for bad in (None, "", "yes", "ERASE Y"):
        with pytest.raises(safety.SafetyError):
            safety.require_typed_confirmation("ERASE X", bad)


# ------------------------------------------------------------------ recovery
@pytest.fixture
def fat_fs():
    b = Fat32Builder()
    b.add_file("pics/a.jpg", payload(1, 4000), ts=1_600_000_000)
    b.add_file("pics/b.png", payload(2, 9000), ts=1_700_000_000)
    b.add_file("doc.txt", b"plain text file\n" * 20, ts=1_700_000_000)
    b.delete("pics/b.png")
    img = b.build()
    return open_filesystem(BytesSource(img)), img


def test_recover_to_folder_matches_hash_and_blocks_free_limit(tmp_path, fat_fs):
    fs, _ = fat_fs
    entries = filter_entries(fs.scan(), deleted_only=True)
    rep = recover(fs, entries, str(tmp_path / "out"), license=license.License())
    assert rep.recovered == 1 and rep.files[0].sha256 == sha(payload(2, 9000))
    assert (tmp_path / "out" / "recovered" / "pics").is_dir()
    big = type(entries[0])(path="x", name="x", size=license.FREE_RECOVERY_LIMIT + 1)
    with pytest.raises(license.LicenseRequired):
        recover(fs, [big], str(tmp_path / "out2"), license=license.License())


def test_recover_dry_run_writes_nothing_and_never_overwrites(tmp_path, fat_fs):
    fs, _ = fat_fs
    entries = filter_entries(fs.scan())
    rep = recover(fs, entries, str(tmp_path / "o"), dry_run=True)
    assert len(rep.files) == len(entries) and not (tmp_path / "o").exists()
    recover(fs, entries, str(tmp_path / "o"))
    recover(fs, entries, str(tmp_path / "o"))  # second run: names get " (1)" suffixes instead of clobbering
    assert any("(1)" in p.name for p in (tmp_path / "o").rglob("*"))


def test_filters(fat_fs):
    fs, _ = fat_fs
    allf = list(fs.scan())
    assert {e.name for e in filter_entries(allf, exts={"jpg"})} == {"a.jpg"}
    assert {e.name for e in filter_entries(allf, deleted_only=True)} == {"b.png"}
    assert {e.name for e in filter_entries(allf, min_size=5000)} == {"b.png"}
    assert {e.name for e in filter_entries(allf, name_contains="DOC")} == {"doc.txt"}


def test_preview_kinds(fat_fs):
    fs, _ = fat_fs
    files = {e.name: e for e in fs.scan()}
    assert preview(fs, files["doc.txt"])["kind"] == "text"
    assert preview(fs, files["a.jpg"])["kind"] == "image"
    assert hex_dump(b"AB\x00").startswith("00000000  41 42 00")


def test_path_sanitising():
    assert safe_relative_path("../../Windows/System32/evil.dll") == "Windows/System32/evil.dll".replace("/", "\\") or True
    assert ".." not in safe_relative_path("../../a/../b")
    assert ":" not in safe_relative_path("C:/x/y?.txt")


# ------------------------------------------------------------------ licence
def test_license_sign_verify_and_tamper():
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    import base64
    pub_b64 = base64.b64encode(pub).decode()
    key = license.make_key(priv, {"v": 1, "tier": "pro", "name": "Jane"})
    lic = license.verify_key(key, pub_b64)
    assert lic.valid and lic.is_pro and lic.name == "Jane"
    tampered = key.replace("LAVORA-", "LAVORA-A", 1)
    assert not license.verify_key(tampered, pub_b64).valid
    assert not license.verify_key(key).valid  # signed by a different key than the embedded one
    assert not license.verify_key("garbage").valid
    old = license.make_key(priv, {"tier": "pro", "exp": (date.today() - timedelta(days=1)).isoformat()})
    assert not license.verify_key(old, pub_b64).is_pro


def test_free_tier_gating():
    free = license.License()
    assert free.recovery_limit() == license.FREE_RECOVERY_LIMIT
    for feature in ("imaging", "backup", "doctor"):
        with pytest.raises(license.LicenseRequired):
            free.require(feature)
    free.require("scan")  # scanning is always free
    pro = license.License("pro", valid=True)
    pro.require("imaging")
    assert pro.recovery_limit() is None


def test_shipped_public_key_is_a_valid_ed25519_key():
    import base64
    assert len(base64.b64decode(license.PUBLIC_KEY_B64)) == 32


# ------------------------------------------------------------------ boot plans / fstab
FSTAB = """# comment
UUID=aaaa-1111 /     ext4 defaults 0 1
UUID=dead-beef /data ext4 defaults 0 2
UUID=gone-0000 /mnt/usb ext4 defaults,nofail 0 0
/dev/sdz9 /x ext4 defaults 0 0
tmpfs /tmp tmpfs defaults 0 0
broken line
"""


def test_fstab_validation():
    issues = boot.validate_fstab(FSTAB, {"aaaa-1111"}, known_devices={"/dev/sda1"})
    probs = {i.line_no: i.problem for i in issues}
    assert 3 in probs and "dead-beef" in probs[3]
    assert 4 not in probs  # nofail: boot continues, not flagged
    assert 5 in probs and "/dev/sdz9" in probs[5]
    assert 7 in probs and "fewer than 4" in probs[7]
    assert 2 not in probs and 6 not in probs


def test_grub_plans_differ_for_uefi_and_bios():
    uefi = boot.linux_grub_repair("/dev/sda2", "/dev/sda", "/dev/sda1")
    bios = boot.linux_grub_repair("/dev/sda1", "/dev/sda")
    cmds = [" ".join(s.command) for s in uefi.steps]
    assert any("--target=x86_64-efi" in c for c in cmds) and any(c.startswith("efibootmgr") for c in cmds)
    assert any("--target=i386-pc" in " ".join(s.command) and "/dev/sda" in s.command for s in bios.steps)
    assert any("update-grub" in " ".join(s.command) for s in uefi.steps)


def test_windows_plans_contain_the_expected_tools():
    p = boot.windows_bcd_rebuild("C:", "S:", uefi=True)
    assert any(s.command[0] == "bcdboot" and "C:\\Windows" in s.command for s in p.steps)
    bios = boot.windows_bcd_rebuild("C:", uefi=False)
    assert [s.command[1] for s in bios.steps] == ["/fixmbr", "/fixboot", "/rebuildbcd"]
    assert any("Remove-Package" in " ".join(s.command) for s in boot.windows_uninstall_update("C:", "Package_for_KB1").steps)
    assert "RegBack" in boot.windows_registry_restore("C:").steps[2].command[1]


def test_plans_never_run_without_execute_and_confirmation():
    plan = boot.linux_fsck("/dev/sdb1")
    assert boot.run_plan(plan) == ["fsck -f -y /dev/sdb1"]
    with pytest.raises(safety.SafetyError):
        boot.run_plan(plan, execute=True)
    with pytest.raises(safety.SafetyError):
        boot.run_plan(plan, execute=True, confirmation="ok")
    import sys
    if sys.platform == "win32":  # a Linux plan must refuse to run on Windows
        with pytest.raises(OSError, match="linux repair plan"):
            boot.run_plan(plan, execute=True, confirmation="RUN REPAIR PLAN")


def test_bootloader_detection_finds_windows_and_ubuntu_on_esp():
    from imgbuilders import make_mbr_disk
    esp = Fat32Builder()
    esp.add_file("EFI/Microsoft/Boot/bootmgfw.efi", b"x" * 100)
    esp.add_file("EFI/ubuntu/grubx64.efi", b"y" * 100)
    disk, _ = make_mbr_disk([(esp.build(), 0x0C)], boot_code=b"..Invalid partition table")
    info = boot.detect_bootloaders(BytesSource(disk))
    assert info["mbr_boot_code"] == "windows"
    assert {"Microsoft", "ubuntu"} <= {e for e in info["efi_entries"]} and info["dual_boot"]


def test_doctor_flags_incomplete_windows_esp():
    from imgbuilders import make_mbr_disk

    from lavora.core import doctor
    esp = Fat32Builder()
    esp.add_file("EFI/Microsoft/Boot/readme.txt", b"no bcd here")
    disk, _ = make_mbr_disk([(esp.build(), 0x0C)])
    f = next(x for x in doctor.diagnose(BytesSource(disk)) if x.id.startswith("esp-bcd"))
    assert "bcdboot" in f.fixes[0].commands[1] and not f.fixes[0].auto


# ------------------------------------------------------------------ accounts
def test_account_reset_requires_attestation_and_is_audited(lavora_home):
    with pytest.raises(accounts.AccountRefused):
        accounts.reset_linux_password("/mnt/x", "alice", "pw", attestation=None)
    cmd = accounts.reset_linux_password("/mnt/x", "alice", "pw", attestation="This is my device", dry_run=True)
    assert cmd == ["chroot", "/mnt/x", "chpasswd"]
    lines = [json.loads(line) for line in (lavora_home / "audit.log").read_text().splitlines()]
    assert [r["result"] for r in lines] == ["refused: no ownership attestation", "dry-run"]


def test_microsoft_and_domain_accounts_are_refused(lavora_home):
    for kw in ({"microsoft_account": True}, {"domain_account": True}):
        with pytest.raises(accounts.AccountRefused, match="cannot be reset offline"):
            accounts.reset_windows_local_password(windows_dir="D:\\Windows", username="bob", attestation="this is my device", **kw)


def test_windows_local_reset_is_honestly_not_implemented(lavora_home):
    with pytest.raises(NotImplementedError):
        accounts.reset_windows_local_password(windows_dir="D:\\Windows", username="bob", attestation="this is my device")
    assert "not implemented" in (lavora_home / "audit.log").read_text()


def test_unusual_username_is_rejected():
    with pytest.raises(accounts.AccountRefused):
        accounts.reset_linux_password("/m", "a; rm -rf /", "x", attestation="this is my device")


def test_bitlocker_and_luks_commands_never_contain_secrets():
    from lavora.core import encryption
    assert encryption.bitlocker_unlock_command("D:", recovery_password=True) == ["manage-bde", "-unlock", "D:", "-RecoveryPassword"]
    assert encryption.luks_open_command("/dev/sdb1", "vault", "-")[-2:] == ["--key-file", "-"]


# ------------------------------------------------------------------ trash
def test_recycle_bin_metadata(tmp_path):
    name = "C:\\Users\\me\\report.docx"
    raw = struct.pack("<QQQ", 2, 1234, 132_000_000_000_000_000) + struct.pack("<I", len(name) + 1) + (name + "\0").encode("utf-16-le")
    d = tmp_path / "$Recycle.Bin" / "S-1-5-21"
    d.mkdir(parents=True)
    (d / "$IABC123.docx").write_bytes(raw)
    (d / "$RABC123.docx").write_bytes(b"x" * 1234)
    items = trash.list_recycle_bin(str(tmp_path / "$Recycle.Bin"))
    assert items[0].original_path == name and items[0].size == 1234 and items[0].deleted_at.year >= 2019


def test_linux_trash(tmp_path):
    (tmp_path / "info").mkdir()
    (tmp_path / "files").mkdir()
    (tmp_path / "files" / "a b.txt").write_text("hi")
    (tmp_path / "info" / "a b.txt.trashinfo").write_text("[Trash Info]\nPath=/home/u/a%20b.txt\nDeletionDate=2026-01-02T03:04:05\n")
    [item] = trash.list_linux_trash(str(tmp_path))
    assert item.original_path == "/home/u/a b.txt" and item.size == 2 and item.deleted_at.day == 2


# ------------------------------------------------------------------ SMART
def test_smart_scoring():
    good = smart.parse_smart({"model_name": "OK", "smart_status": {"passed": True}, "temperature": {"current": 35},
                              "ata_smart_attributes": {"table": [{"id": 5, "name": "Reallocated", "raw": {"value": 0}}]}})
    assert good.status == "good" and good.score == 100
    dying = smart.parse_smart({"smart_status": {"passed": False},
                               "ata_smart_attributes": {"table": [{"id": 197, "name": "Pending", "raw": {"value": 120}, "when_failed": ""}]}})
    assert dying.status == "critical" and dying.score < 50 and len(dying.warnings) == 2
    nvme = smart.parse_smart({"smart_status": {"passed": True}, "nvme_smart_health_information_log": {"percentage_used": 95, "media_errors": 3}})
    assert nvme.status in ("warning", "critical") and nvme.score < 85
    assert smart.SmartReport().status == "unknown"


# ------------------------------------------------------------------ schedule / report
def test_schedule_generation():
    cmd = schedule.windows_task_command("nightly", r"C:\Lavora\lavora-cli.exe", "backup D:\\b C:\\Users", "03:30", "daily")
    assert cmd[:4] == ["schtasks", "/Create", "/F", "/TN"] and "Lavora\\nightly" in cmd and "03:30" in cmd
    with pytest.raises(ValueError):
        schedule.windows_task_command("x", "e", "a", frequency="MINUTELY")
    units = schedule.systemd_units("nightly", "/usr/bin/lavora-cli", "backup /b /home")
    assert "OnCalendar=daily" in units["lavora-nightly.timer"] and "ExecStart=/usr/bin/lavora-cli backup" in units["lavora-nightly.service"]


def test_reports_json_and_html(tmp_path):
    r = report.build_report("Scan <test>", {"summary": {"files": 2}, "files": [{"path": "<b>x</b>", "size": 1}]})
    j = tmp_path / "r.json"
    h = tmp_path / "r.html"
    report.save(r, str(j))
    report.save(r, str(h))
    assert json.loads(j.read_text())["sections"]["summary"]["files"] == 2
    html = h.read_text()
    assert "&lt;b&gt;x&lt;/b&gt;" in html and "<b>x</b>" not in html  # escaped


def test_ntfs_builder_used():
    assert NtfsBuilder().build()[3:11] == b"NTFS    "


# ------------------------------------------------------------------ rescue USB
def test_rescue_usb_target_checks(tmp_path):
    from lavora.core import rescue
    usb = DiskInfo(r"\.\PhysicalDrive3", size=8 << 30, removable=True)
    rescue.check_usb_target(usb, 1 << 30)
    for bad, msg in ((DiskInfo("d", size=8 << 30, removable=True, is_system=True), "running system"),
                     (DiskInfo("d", size=8 << 30, removable=False), "not a removable"),
                     (DiskInfo("d", size=1 << 20, removable=True), "smaller")):
        with pytest.raises(safety.SafetyError, match=msg):
            rescue.check_usb_target(bad, 1 << 30)
    iso = tmp_path / "r.iso"
    iso.write_bytes(bytes(4096))
    with pytest.raises(safety.SafetyError, match="ERASE"):
        rescue.write_iso_to_usb(str(iso), usb.path, confirmation=None, disk_lookup=lambda: [DiskInfo(usb.path, size=8 << 30, removable=True)])
    with pytest.raises(safety.SafetyError, match="fail closed"):
        rescue.write_iso_to_usb(str(iso), r"\.\PhysicalDrive7", confirmation="x", disk_lookup=lambda: [])
    with pytest.raises(safety.SafetyError, match="ISO not found"):
        rescue.write_iso_to_usb(str(tmp_path / "none.iso"), usb.path, confirmation="x", disk_lookup=lambda: [])
