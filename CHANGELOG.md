# Changelog

## 0.2.0 - unreleased
### Added
- **Recover everything** (GUI page, `lavora-cli autorecover`): finds volumes (partition table -> filesystem at start -> lost-volume scan), opens each with fallbacks
  (backup boot sector, MFT mirror, backup superblock), recovers all files, falls back to signature carving, writes an HTML report of every method tried.
- Partial-image awareness: files touching never-copied areas are flagged and recovered separately; "Recover what's copied so far" button.
- Handles multi-terabyte disks without hanging: streaming scans, cancel everywhere, resume points, bounded memory (see ROADMAP).
- Virtual RAID 0/1/5/6/10 with degraded reads and parameter auto-detection; `raid-detect`; GUI RAID builder.
- Virtual disk containers: dynamic VHD, VHDX, VMDK, VDI.
- Saved scan sessions, recovery-chance summary, `hex` / `find` / `sector-write`, GUI Disk editor, tile icons.
### Changed
- Imaging no longer re-reads the whole disk to hash it unless `--hash` is given.
- `recover` and scans stream; `--save`, `--limit`, `--session`, `--verbose`, `--start` options added.

## 0.1.0 - 2026-10-22
First build. Everything below was verified against **generated disk images** by `pytest`; see `docs/test-matrix.md` for what
still needs a VM / real hardware.

### Added
- Core engine: read-only sources, MBR/GPT parsing and rebuilding, lost-partition scan, NTFS / FAT12-16-32 / exFAT / ext2-4 readers
  with deleted-file recovery and overwrite estimates, signature carver, imaging with bad-sector map, SMART parser + surface scan,
  undo snapshots, Doctor, backup (incremental, AES-256-GCM, test-restore verification), boot/repair plans, fstab validation,
  Recycle Bin / Linux trash / shadow copy listing, owner-only account gate with audit log, offline Ed25519 licence hook.
- `lavora-cli` command line and PySide6 GUI shell.
- Installer (Inno Setup, modelled on Zapi), portable ZIP, `installer\build.ps1`.
- Docs: roadmap, architecture, Windows boot repair options, test matrix, code signing, third-party licence plan.

### Known gaps
See `ROADMAP.md`: Rescue ISO not built, Windows local password reset not implemented, SFTP/cloud destinations absent,
validation against real Windows-formatted NTFS pending (parsers tested only against generated, spec-based volumes),
placeholder artwork, unsigned binaries.
