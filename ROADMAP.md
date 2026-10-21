# Roadmap

Legend: **done** = implemented and covered by automated tests on generated images; **partial** = works but limited (see note);
**stub** = honest placeholder (plans/doc only); **todo**.

## Phase 1 - MVP (done)
- done: scaffold, brand constants, single-source version, i18n hook, read-only block sources (image, device, VHD, partition view)
- done: disk/volume enumeration (PowerShell on Windows, lsblk on Linux; needs admin to see physical disks)
- done: read-only imaging with bad-sector map, multi-pass retry, pause/resume/cancel, gzip, clone + hash verify
- done: NTFS + FAT32 deleted-file scan, preview, filters, recover-to-other-volume (fails closed), free-tier size cap
- done: CLI (`lavora-cli`) + GUI (home wizard tiles, Recover, Doctor, Image, Backup, Boot plans)
- done: installer from the Zapi template, portable ZIP, `build.ps1`, tests, CI workflow

## Phase 2 (pulled forward; done where it was cheap to test)
- done: exFAT, ext2/3/4 parsers (ext3/4 deleted recovery is best effort: no journal replay)
- done: signature carving (JPG PNG GIF PDF DOCX/XLSX/PPTX/ZIP MP4/MOV MP3 SQLite BMP RAR 7z), extensible table
- done: lost-partition scan + MBR/GPT rebuild, NTFS/FAT32 boot-sector and GPT-copy repair (undoable)
- partial: SMART diagnostics (parser + score tested; needs an installed/bundled `smartctl`), surface scan
- done: backup/restore, incremental, AES-256-GCM, verification by test-restore, schedule generation (Task Scheduler / systemd)
- todo: SFTP/cloud destinations (needs an SSH library: approval pending), Btrfs/XFS, NTFS compressed/EFS/attribute lists

## Phase 3
- stub: Rescue ISO (recipe in `rescue/`, **not built or booted**); USB writer implemented and safety-tested, never run on real hardware
- partial: Linux GRUB/chroot/initramfs/packages/fsck/kernel-rollback = **command plans**, fstab validator (tested); execution path exists but only with typed confirmation, on Linux
- partial: offline password reset: attestation + audit + refusal rules + Linux chroot `chpasswd`; **Windows local reset not implemented**
- done: Lavora Doctor (findings, ranking, apply with dry-run default, undo)
- todo: offline ClamAV, Rescue touch GUI, file manager, Wi-Fi setup

## Phase 4
- partial: Windows boot repair = plans (see `docs/windows-boot-repair.md`); spike not executed; WinPE local-build wizard todo
- partial: dual-boot **detection** done; repair is the GRUB/BCD plans
- partial: bare-metal restore = image clone onto a device with typed confirmation; Rescue-USB flow todo
- done: scheduled backup generation; licence hook (Ed25519, offline) with Free/Pro gating
- todo: Tamil/Hindi catalogues, code-signing certificate, real artwork, payment/licence portal (out of scope)
