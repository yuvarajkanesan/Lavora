"""Lavora Doctor: scan a disk or image, list ranked findings, and offer one-click fixes that can be undone.

Automatic fixes only ever rewrite small, well-understood structures (partition tables, boot sectors) from a surviving
copy, and always go through undo.write_regions (snapshot first, dry-run by default). Anything bigger is reported as a
"plan": the exact commands to run, never executed automatically.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

from .device import SECTOR, BlockSource, PartitionView
from .fs.base import FsError
from .fs.fat import FatFilesystem
from .partitions import (
    GPT_ESP,
    Partition,
    build_gpt_pair,
    build_mbr,
    gpt_repair_regions,
    identify_fs,
    parse_gpt,
    parse_mbr,
    scan_lost_partitions,
)
from .smart import SmartReport
from .undo import WriteResult, write_regions

SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}


@dataclass
class Fix:
    id: str
    title: str
    description: str
    regions: list[tuple[int, bytes]] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)  # for plan-only fixes

    @property
    def auto(self) -> bool:
        return bool(self.regions)


@dataclass
class Finding:
    id: str
    severity: str  # critical | warning | info
    title: str
    detail: str
    fixes: list[Fix] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"id": self.id, "severity": self.severity, "title": self.title, "detail": self.detail,
                "fixes": [{"id": f.id, "title": f.title, "auto": f.auto, "commands": f.commands} for f in self.fixes]}


def _ntfs_ok(boot: bytes) -> bool:
    return boot[3:11] == b"NTFS    " and boot[510:512] == b"\x55\xaa"


def _fat32_ok(boot: bytes) -> bool:
    return boot[82:90] == b"FAT32   " and boot[510:512] == b"\x55\xaa"


def _check_volume(source: BlockSource, p: Partition, label: str) -> list[Finding]:
    out: list[Finding] = []
    boot = source.read_at(p.start, SECTOR)
    fs = identify_fs(source, p.start)
    want = p.fs or fs
    if fs:
        want = fs
    # NTFS: backup boot sector lives in the last sector of the volume
    backup_pos = p.start + p.size - SECTOR
    backup = source.read_at(backup_pos, SECTOR)
    if not fs and _ntfs_ok(backup):
        out.append(Finding(f"ntfs-boot-{p.index}", "critical", f"{label}: NTFS boot sector is damaged",
                           "The first sector of the volume is unreadable as NTFS, but the backup copy at the end of "
                           "the volume is intact.",
                           [Fix("restore-ntfs-boot", "Restore the NTFS boot sector from its backup",
                                "Copies the last sector of the volume over the first sector.",
                                [(p.start, backup)])]))
        return out
    if not fs and p.start + 7 * SECTOR <= source.size and _fat32_ok(source.read_at(p.start + 6 * SECTOR, SECTOR)):
        out.append(Finding(f"fat32-boot-{p.index}", "critical", f"{label}: FAT32 boot sector is damaged",
                           "The backup boot sector (sector 6) is intact.",
                           [Fix("restore-fat32-boot", "Restore the FAT32 boot sector from sector 6",
                                "Copies sector 6 over sector 0.", [(p.start, source.read_at(p.start + 6 * SECTOR, SECTOR))])]))
        return out
    if want == "ntfs":
        g = struct.unpack_from("<Q", boot, 48)[0]
        bps, spc = struct.unpack_from("<HB", boot, 11)
        if source.read_at(p.start + g * bps * spc, 4) != b"FILE":
            out.append(Finding(f"ntfs-mft-{p.index}", "critical", f"{label}: the NTFS $MFT is damaged",
                               "The Master File Table is not where the boot sector says it is. Files cannot be listed "
                               "normally.",
                               [Fix("plan-ntfs-chkdsk", "Check the volume offline (does not modify until you run it)",
                                    "From Windows or WinPE run chkdsk on the volume; to save files first, use a "
                                    "signature carve (lavora carve).", commands=["chkdsk X: /scan", "lavora carve <image> --out <other-drive>"])]))
        elif boot[3:11] == b"NTFS    " and not _ntfs_ok(backup):
            out.append(Finding(f"ntfs-backup-{p.index}", "info", f"{label}: NTFS backup boot sector is missing",
                               "Not urgent; the primary boot sector is fine.", []))
    if not fs and not out and (p.size >= SECTOR * 64):
        out.append(Finding(f"fs-unknown-{p.index}", "warning", f"{label}: filesystem not recognised",
                           "No intact boot sector or superblock was found. The data may still be recoverable with a "
                           "deep scan or signature carve.", [Fix("plan-carve", "Recover files by signature",
                                                                 "Carves common file types without a filesystem.",
                                                                 commands=["lavora carve <image> --out <other-drive>"])]))
    return out


def _check_esp(source: BlockSource, p: Partition) -> list[Finding]:
    try:
        view = PartitionView(source, p.start, p.size)
        fs = FatFilesystem(view)
        paths = {e.path.lower() for e in fs.scan() if not e.deleted}
    except (FsError, OSError, struct.error):
        return []
    if "efi/microsoft/boot/bcd" in paths and "efi/microsoft/boot/bootmgfw.efi" in paths or "efi/boot/bootx64.efi" in paths:
        return []
    if any(path.startswith("efi/microsoft") for path in paths):
        return [Finding(f"esp-bcd-{p.index}", "critical", "Windows boot files are incomplete on the EFI System Partition",
                        "EFI/Microsoft/Boot/BCD or bootmgfw.efi is missing.",
                        [Fix("plan-bcdboot", "Rebuild the boot files from the Windows folder",
                             "Run from Windows Recovery / WinPE (Windows boot repair needs Microsoft tooling; see "
                             "docs/windows-boot-repair.md). Nothing is changed until you run these.",
                             commands=["diskpart  (assign a letter to the ESP, e.g. S:)",
                                       "bcdboot C:\\Windows /s S: /f UEFI"])])]
    return []


def diagnose(source: BlockSource, *, smart: SmartReport | None = None) -> list[Finding]:
    findings: list[Finding] = []
    size = source.size
    mbr = source.read_at(0, SECTOR)
    has_gpt_sig = source.read_at(SECTOR, 8) == b"EFI PART"
    protective = any(p.type_desc == "gpt-protective" for p in parse_mbr(mbr))
    volumes: list[Partition] = []

    if protective or has_gpt_sig or parse_gpt(source).primary_ok or parse_gpt(source).backup_ok:
        info = parse_gpt(source)
        if info.primary_ok and info.backup_ok:
            pass
        elif info.primary_ok != info.backup_ok:
            bad = "primary" if not info.primary_ok else "backup"
            findings.append(Finding("gpt-copy", "warning", f"GPT {bad} header/table is damaged",
                                    f"The {bad} copy fails its checks ({info.primary_error or info.backup_error}); "
                                    "the other copy is intact.",
                                    [Fix("repair-gpt", "Rebuild the damaged GPT copy from the good one",
                                         "Rewrites only the GPT header and entry array of the damaged copy.",
                                         gpt_repair_regions(source))]))
        else:
            found = scan_lost_partitions(source)
            fixes = []
            if found:
                parts = [Partition(i + 1, f.start, f.size, "found", f.fs, f.fs) for i, f in enumerate(found)]
                prim, arr, bak = build_gpt_pair(size, parts)
                total_lba = size // SECTOR
                esect = len(arr) // SECTOR
                fixes.append(Fix("rebuild-gpt", "Rebuild the GPT from the volumes found on the disk",
                                 f"Creates a new GPT describing {len(found)} volume(s) found by scanning.",
                                 [(SECTOR, prim), (2 * SECTOR, arr), ((total_lba - 1 - esect) * SECTOR, arr),
                                  ((total_lba - 1) * SECTOR, bak)]))
            findings.append(Finding("gpt-lost", "critical", "Both GPT copies are damaged",
                                    f"{len(found)} volume(s) can still be found by scanning." if found
                                    else "No volumes were found by scanning.", fixes))
        volumes = info.entries or [Partition(i + 1, f.start, f.size, "found", f.fs, f.fs)
                                   for i, f in enumerate(scan_lost_partitions(source))]
    elif mbr[510:512] == b"\x55\xaa" and parse_mbr(mbr):
        volumes = parse_mbr(mbr)
    else:
        if identify_fs(source, 0):
            volumes = [Partition(1, 0, size, "none", fs=identify_fs(source, 0))]
        else:
            found = scan_lost_partitions(source)
            fixes = []
            if found and len(found) <= 4:
                parts = [Partition(i + 1, f.start, f.size, "found", f.fs, f.fs, bootable=i == 0)
                         for i, f in enumerate(found)]
                fixes.append(Fix("rebuild-mbr", "Rebuild the MBR partition table from the volumes found",
                                 f"Writes a new partition table for {len(found)} volume(s); boot code is kept.",
                                 [(0, build_mbr(parts, boot_code=mbr[:446] if mbr[:446] != bytes(446) else None))]))
            findings.append(Finding("no-table", "critical", "No valid partition table",
                                    f"The disk has no readable MBR/GPT. {len(found)} volume(s) found by scanning.",
                                    fixes))
            volumes = [Partition(i + 1, f.start, f.size, "found", f.fs, f.fs) for i, f in enumerate(found)]

    for p in volumes:
        p.fs = identify_fs(source, p.start) or p.fs
        findings += _check_volume(source, p, f"Partition {p.index}")
        if p.fs == "fat32" and (p.type_desc == GPT_ESP or p.size < 600 * 1024 * 1024):
            findings += _check_esp(source, p)

    if smart and smart.available:
        sev = "critical" if smart.status == "critical" else "warning"
        if smart.status in ("critical", "warning"):
            findings.append(Finding("smart", sev, f"Drive health is {smart.status} (score {smart.score}/100)",
                                    "; ".join(smart.warnings) or "SMART attributes are degraded.",
                                    [Fix("plan-image", "Image the drive before anything else",
                                         "A failing drive should be cloned, then repaired/recovered from the copy.",
                                         commands=["lavora image <device> <file.img>"])]))
    findings.sort(key=lambda f: (SEVERITY_RANK[f.severity], not any(x.auto for x in f.fixes)))
    return findings


def apply_fix(target: str, fix: Fix, *, dry_run: bool = True, store: str | None = None) -> WriteResult:
    """Apply an automatic fix to `target` (image path or device). Dry-run unless told otherwise; undoable."""
    if not fix.auto:
        raise ValueError(f"'{fix.title}' is a plan, not an automatic fix. Run its commands yourself.")
    return write_regions(target, fix.regions, fix.title, dry_run=dry_run, store=store)

