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

## Added after the first build (commercial-tool parity round)
- done: **multi-TB scale (tested on a synthetic and a real 5 TiB sparse image)**: streaming scans with bounded memory, cancel/Ctrl+C
  everywhere (scan, recover, carve, partition scan, surface scan, imaging, backup, doctor), resume points (`carve --start`, imaging map,
  backup chunk reuse), sampled overwrite estimates, popcount bitmap checks, bisect reads in fragmented files, lazy FAT chains,
  hash-after-imaging is now opt-in (`--hash`), a full destination stops the run
- done: **virtual RAID** 0/1/5/6/10 from images or disks, degraded reads (XOR and GF(256) P+Q), automatic detection of level, chunk size,
  layout and member order (`raid-detect`, GUI RAID builder); `raid:` spec strings work as a source for every command
- done: **virtual disks**: dynamic VHD, VHDX, VMDK (sparse + flat descriptor), VDI open like any image
- done: **saved scan sessions** (`scan --save`, `recover --session`), recovery-chance summary after every scan
- done: **disk editor**: hex view/search/decode in GUI and CLI (`hex`, `find`), `sector-write` (dry run, undo snapshot)
- partial: RAID auto-detection is scored on partition tables, filesystem structures far apart on the volume, and file head/tail magic;
  arrays holding only random data (encrypted volumes, databases) cannot be identified; tested on generated arrays only
- todo (not started): ReFS, HFS+/APFS, XFS, UFS, Btrfs parsers (carving already works on any filesystem); recovery over the network from
  remote machines (needs your approval: network code + a remote agent); differencing VHD/VHDX/VMDK chains; BitLocker unlock with
  your own key done natively (today: manage-bde/cryptsetup wrappers); RAID 6 parameter auto-detection beyond the md layout,
  non-standard levels (1E, 5E, 5EE, 6E)

## Latest additions
- done: **Recover everything** (one click / `autorecover`): volume discovery (partition table -> filesystem at start -> lost-volume scan), per-volume fallback
  chain (primary metadata -> backup boot sector -> $MFT mirror -> combinations), carving fallback, de-duplication, HTML report of every method tried
- done: partial images: unread-area map, incomplete files kept apart, "Recover everything from what's copied so far"
- done: **only photos / documents / videos / audio** (`--only`, GUI selector); carving adds WAV, AVI, WebP; HEIC/MOV/3GP/M4A named by brand
- done: **Check disk health** (page + `health` command): read-only speed / slow / unreadable test with verdict, plus SMART when available
- done: imaging hardening for failing USB disks: no zero-fill preallocation, free-space check, dead-zone skipping, slow-area deferral, bus-reset retries
- todo: ext/XFS journal-based undelete, RAID auto-detection beyond md layouts, native BitLocker unlock

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
