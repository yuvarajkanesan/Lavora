# Lavora

*Lavora - get your system working again.* A Windows + Linux recovery toolkit by Lavati Softwares ("lavora" is Italian for "works").

- **Recover** deleted files (NTFS, FAT12/16/32, exFAT, ext2/3/4) or carve by file type; preview first; output goes to a *different* drive.
- **Image** failing drives (bad-sector map, retries, resume) and work from the image.
- **Repair** partition tables and boot sectors (undoable) and get exact Windows/Linux boot-repair command plans.
- **Back up** with incremental, compressed, optionally AES-256 encrypted backups and a test-restore check.
- Read-only by default, dry-run first, typed confirmation for anything destructive, no telemetry, no encryption bypass.

## Run from source
    python -m pip install -r requirements-dev.txt
    python -m pytest                      # generated disk images, no admin / physical disks needed
    python -m lavora                      # GUI
    python -m lavora scan disk.img --deleted-only
    python -m lavora recover disk.img --out E:\recovered --all

## Build
    installer\build.ps1                   # tests + PyInstaller + selftest + Inno Setup + portable zip -> dist\

See `docs/` (architecture, windows-boot-repair, test-matrix, code-signing) and `ROADMAP.md` for what is done vs stubbed.
