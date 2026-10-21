# Changelog

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
