# Windows boot repair: options, trade-offs, recommendation

> **Status: desk research only.** The brief asks for a short spike (a broken-BCD VM repaired by each option). No VM or
> admin rights were available when this was written, so **none of the options below has been exercised end to end.** Treat
> the recommendation as a hypothesis to confirm in Phase 4 with the scripted-breakage matrix in `test-matrix.md`.

## What "repair" means here
A Windows machine that will not boot is usually one of: damaged/missing **BCD** store, missing **bootmgfw.efi** or
**bootmgr** on the EFI System Partition (or System Reserved), damaged **MBR/GPT** or **boot sector**, a bad
**update/driver**, or damaged **registry hives**. Partition tables and boot sectors Lavora already fixes itself (tested on
images). The rest needs Microsoft's own tools or a faithful re-implementation of their output.

## Options

| | A. Linux Rescue edits the BCD itself | B. WinPE / WinRE based environment | C. Run tools from the broken PC's own WinRE | D. Plans only (current) |
|---|---|---|---|---|
| How | Mount the ESP + Windows volume (ntfs-3g), write a BCD registry hive (hivex or own writer) from a template | Boot WinPE/WinRE carrying `bcdboot`, `bootrec`, `dism`, `sfc`, `reagentc` plus the Lavora CLI | Guide the user to *Shift+Restart > Troubleshoot > Command Prompt* and run printed commands | Print exact commands (`lavora-cli plan windows-bcd`), user runs them |
| Fidelity | Low-medium: BCD element GUIDs/entries must match what `bcdboot` writes; subtle mistakes brick boot | **Highest**: the real Microsoft tools | Highest, but only if WinRE itself still boots | Same as C |
| Works when WinRE is gone | Yes | Yes (own media) | No | No |
| Data tasks (imaging, recovery, ntfs-3g) | Yes, same environment | Weak (no ext4/Btrfs, limited drivers) | Weak | n/a |
| Licensing | Free (GPL tools stay separate processes) | **Restricted**: WinPE comes from the Windows ADK add-on; its licence lets you build WinPE for repairing/deploying Windows, but *redistributing a ready-made WinPE image* is not generally permitted. Safe route: build it **locally on the user's machine from their own ADK/WinRE.wim**. **Needs a legal check before anything ships.** | None (uses what is installed) | None |
| Effort | High, risky | Medium (script around `copype`/`MakeWinPEMedia` or reuse `C:\Windows\System32\Recovery\WinRE.wim`) | Low | Done |

## Recommendation
1. **Now (done):** option D. `lavora-cli plan windows-bcd|windows-sfc|windows-registry` and the Doctor's "plan" fixes print the
   exact commands and the backups to take first; nothing runs automatically. Partition table / boot sector repairs are real
   and undoable.
2. **Phase 4:** option B in its *local-build* form: a "Create Windows repair media" wizard that uses the **user's own** ADK or
   `WinRE.wim` to produce WinPE media containing the Lavora CLI. Nothing from Microsoft is redistributed in our installer or ISO.
3. Keep the Linux Rescue ISO for imaging, recovery, GRUB/Linux repair and password reset; do **not** ship option A's BCD writer
   unless it is verified against `bcdboot` output on real VMs for BIOS and UEFI.

## Before any change
Lavora takes automatic backups first: undo snapshots for sector writes, and the plans start with
`bcdedit /export <folder>\bcd.bak` (BCD) or copy the hive (registry). RegBack is empty by default on Windows 10 1803+; use a
System Restore point or backup instead.

## Open questions (for the owner)
* Legal review of WinPE/ADK terms for the "local build" wizard (and whether a download helper for the ADK is acceptable).
* Secure Boot: media signed by the Microsoft CA is required to boot WinPE under Secure Boot; our Linux ISO needs a signed shim
  (Debian's shim works) or the user must disable Secure Boot. Document in the Rescue guide.
