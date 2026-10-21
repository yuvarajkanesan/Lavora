"""Boot and system repair: bootloader detection, fstab validation, and command *plans* for Windows and Linux repair.

Plans are ordered command lists with explanations. They are shown to the user and only run through run_plan(), which
needs an explicit execute flag plus the typed phrase, logs to the audit log, and refuses to run a plan on the wrong OS.
Why plans: rebuilding a BCD or reinstalling GRUB needs the real Microsoft / GRUB tools inside the target system; the
safest honest design is to generate exact commands (and the snapshots to take first) rather than imitate those tools.
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

from .accounts import audit
from .device import SECTOR, BlockSource, PartitionView
from .fs.base import FsError
from .fs.fat import FatFilesystem
from .partitions import GPT_ESP, read_partitions
from .safety import require_typed_confirmation


@dataclass
class Step:
    command: list[str]
    why: str


@dataclass
class Plan:
    title: str
    os_family: str  # "windows" | "linux"
    steps: list[Step] = field(default_factory=list)
    prerequisites: list[str] = field(default_factory=list)

    def as_text(self) -> str:
        lines = [self.title, ""] + [f"  needs: {p}" for p in self.prerequisites]
        for i, s in enumerate(self.steps, 1):
            lines.append(f"{i}. {' '.join(s.command)}\n     {s.why}")
        return "\n".join(lines)


# ---------------------------------------------------------------- Windows plans
def windows_bcd_rebuild(windows_drive: str = "C:", esp_letter: str = "S:", uefi: bool = True) -> Plan:
    p = Plan("Rebuild the Windows boot configuration (BCD)", "windows",
             prerequisites=["Boot Windows Recovery Environment / WinPE as administrator",
                            "Take a backup first: bcdedit /export <folder>\\bcd.bak"])
    if uefi:
        p.steps = [Step(["diskpart"], f"assign letter {esp_letter} to the EFI System Partition (select disk/partition, assign)"),
                   Step(["bcdboot", f"{windows_drive}\\Windows", "/s", esp_letter, "/f", "UEFI"],
                        "re-create EFI\\Microsoft\\Boot\\BCD and bootmgfw.efi from the Windows installation")]
    else:
        p.steps = [Step(["bootrec", "/fixmbr"], "write a standard Windows MBR boot code (partition table untouched)"),
                   Step(["bootrec", "/fixboot"], "write a new boot sector to the system partition"),
                   Step(["bootrec", "/rebuildbcd"], "scan for Windows installations and rebuild the BCD store")]
    return p


def windows_offline_sfc_dism(windows_drive: str = "C:") -> Plan:
    return _sfc_plan(windows_drive)


def _sfc_plan(drive: str) -> Plan:
    return Plan("Offline system file repair", "windows",
                steps=[Step(["sfc", "/scannow", f"/offbootdir={drive}\\", f"/offwindir={drive}\\Windows"],
                            "verify protected system files against the component store"),
                       Step(["dism", f"/Image:{drive}\\", "/Cleanup-Image", "/RestoreHealth"],
                            "repair the component store (offline this may need /Source:WIM:<install.wim>:1)")],
                prerequisites=["WinPE / Recovery Environment"])


def windows_uninstall_update(drive: str, package_name: str) -> Plan:
    return Plan("Uninstall a bad Windows update offline", "windows",
                steps=[Step(["dism", f"/Image:{drive}\\", "/Get-Packages"], "list installed packages and find the KB"),
                       Step(["dism", f"/Image:{drive}\\", "/Remove-Package", f"/PackageName:{package_name}"],
                            "remove the package; pending operations may need /Cleanup-Image /RevertPendingActions")])


def windows_driver_change(drive: str, remove_inf: str | None = None, add_path: str | None = None) -> Plan:
    steps = []
    if remove_inf:
        steps.append(Step(["dism", f"/Image:{drive}\\", "/Remove-Driver", f"/Driver:{remove_inf}"], "remove the offline driver"))
    if add_path:
        steps.append(Step(["dism", f"/Image:{drive}\\", "/Add-Driver", f"/Driver:{add_path}", "/Recurse"], "inject drivers"))
    return Plan("Offline driver removal / injection", "windows", steps)


def windows_registry_restore(drive: str) -> Plan:
    cfg = f"{drive}\\Windows\\System32\\config"
    steps = [Step(["copy", f"{cfg}\\{h}", f"{cfg}\\{h}.lavora-bak"], f"keep a copy of {h} before replacing") for h in
             ("SYSTEM", "SOFTWARE")]
    steps += [Step(["copy", f"{cfg}\\RegBack\\{h}", f"{cfg}\\{h}"], f"restore {h} from RegBack") for h in ("SYSTEM", "SOFTWARE")]
    return Plan("Restore registry hives from backup", "windows", steps,
                ["RegBack is empty by default on Windows 10 1803+ unless enabled; otherwise use a System Restore point"])


# ---------------------------------------------------------------- Linux plans
def _pkg_fix(distro: str) -> list[list[str]]:
    return {"debian": [["apt-get", "-f", "install"], ["dpkg", "--configure", "-a"]],
            "fedora": [["dnf", "distro-sync", "--allowerasing"]],
            "arch": [["pacman", "-Syu"]]}[distro]


def linux_chroot_prefix(root_dev: str, efi_dev: str | None = None, mnt: str = "/mnt/lavora") -> list[Step]:
    steps = [Step(["mkdir", "-p", mnt], "mount point"), Step(["mount", root_dev, mnt], "mount the broken system's root")]
    if efi_dev:
        steps.append(Step(["mount", efi_dev, f"{mnt}/boot/efi"], "mount the EFI System Partition"))
    for d in ("dev", "proc", "sys", "run"):
        steps.append(Step(["mount", "--rbind", f"/{d}", f"{mnt}/{d}"], f"expose /{d} inside the chroot"))
    return steps


def linux_grub_repair(root_dev: str, disk: str, efi_dev: str | None = None, *, distro: str = "debian") -> Plan:
    uefi = efi_dev is not None
    mnt = "/mnt/lavora"
    steps = linux_chroot_prefix(root_dev, efi_dev, mnt)
    cfg = {"debian": ["update-grub"], "fedora": ["grub2-mkconfig", "-o", "/boot/grub2/grub.cfg"],
           "arch": ["grub-mkconfig", "-o", "/boot/grub/grub.cfg"]}[distro]
    if uefi:
        inst = ["grub-install", "--target=x86_64-efi", "--efi-directory=/boot/efi", "--recheck"]
    else:
        inst = ["grub-install", "--target=i386-pc", "--recheck", disk]
    steps += [Step(["chroot", mnt] + inst, "reinstall GRUB (" + ("UEFI" if uefi else "BIOS") + ")"),
              Step(["chroot", mnt] + cfg, "regenerate the GRUB menu (also detects Windows for dual boot)")]
    if uefi:
        steps.append(Step(["efibootmgr", "-v"], "check the firmware has a boot entry; add one with efibootmgr -c if not"))
    return Plan("Reinstall / repair GRUB", "linux", steps, ["root", "run from Lavora Rescue or another Linux system"])


def linux_initramfs(root_dev: str, distro: str = "debian") -> Plan:
    cmd = {"debian": ["update-initramfs", "-u", "-k", "all"], "fedora": ["dracut", "--force", "--regenerate-all"],
           "arch": ["mkinitcpio", "-P"]}[distro]
    return Plan("Regenerate the initramfs", "linux", linux_chroot_prefix(root_dev) + [Step(["chroot", "/mnt/lavora"] + cmd, "rebuild")])


def linux_package_repair(root_dev: str, distro: str = "debian") -> Plan:
    steps = linux_chroot_prefix(root_dev)
    steps += [Step(["chroot", "/mnt/lavora"] + c, "repair broken packages") for c in _pkg_fix(distro)]
    return Plan("Fix broken packages (chroot)", "linux", steps, ["network access inside the chroot (copy /etc/resolv.conf)"])


def linux_fsck(device: str) -> Plan:
    return Plan("Check and repair a Linux filesystem", "linux",
                [Step(["fsck", "-f", "-y", device], "the filesystem must be unmounted; -y repairs without asking")])


def linux_kernel_rollback(root_dev: str, version: str, distro: str = "debian") -> Plan:
    boot = {"debian": ["grub-set-default", f"Advanced options for Ubuntu>Ubuntu, with Linux {version}"],
            "fedora": ["grubby", "--set-default", f"/boot/vmlinuz-{version}"],
            "arch": ["pacman", "-U", f"/var/cache/pacman/pkg/linux-{version}-x86_64.pkg.tar.zst"]}[distro]
    return Plan("Boot an older kernel", "linux", linux_chroot_prefix(root_dev) + [Step(["chroot", "/mnt/lavora"] + boot, "select the older kernel")])


# ---------------------------------------------------------------- fstab
@dataclass
class FstabIssue:
    line_no: int
    line: str
    problem: str


def validate_fstab(text: str, known_uuids: set[str], known_labels: frozenset[str] | set[str] = frozenset(),
                   known_devices: frozenset[str] | set[str] = frozenset()) -> list[FstabIssue]:
    """Flag entries that will fail to mount at boot (missing UUID/label/device, bad fields) so they can be fixed."""
    issues = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        f = line.split()
        if len(f) < 4:
            issues.append(FstabIssue(n, raw, "fewer than 4 fields"))
            continue
        spec, mnt, fstype, opts = f[:4]
        if fstype in ("swap",) or spec in ("tmpfs", "proc", "sysfs", "devpts") or fstype in ("tmpfs", "proc", "sysfs", "devpts", "nfs", "cifs"):
            continue
        optset = set(opts.split(","))
        optional = bool(optset & {"nofail", "noauto"})
        if spec.startswith("UUID=") and spec[5:].strip('"') not in known_uuids:
            issues.append(FstabIssue(n, raw, f"UUID {spec[5:]} not found on any disk" + ("" if not optional else " (nofail set)")))
        elif spec.startswith("LABEL=") and spec[6:].strip('"') not in known_labels:
            issues.append(FstabIssue(n, raw, f"label {spec[6:]} not found"))
        elif spec.startswith("/dev/") and known_devices and spec not in known_devices:
            issues.append(FstabIssue(n, raw, f"device {spec} does not exist"))
        if mnt != "none" and not mnt.startswith("/"):
            issues.append(FstabIssue(n, raw, "mount point is not an absolute path"))
    return [i for i in issues if "(nofail set)" not in i.problem]


# ---------------------------------------------------------------- detection
def detect_bootloaders(source: BlockSource) -> dict:
    """Identify the MBR boot code and the OS folders on an EFI System Partition (read-only)."""
    mbr = source.read_at(0, SECTOR)
    code = mbr[:446]
    kind = "empty" if code == bytes(446) else "grub" if b"GRUB" in code else \
        "windows" if b"Invalid partition table" in code or b"Missing operating system" in code else "unknown"
    out: dict[str, Any] = {"mbr_boot_code": kind, "efi_entries": [], "dual_boot": False}
    try:
        _scheme, parts = read_partitions(source)
    except (OSError, ValueError):
        return out
    for p in parts:
        if p.type_desc == GPT_ESP or (p.fs == "fat32" and p.size < 1024 * 1024 * 1024):
            try:
                fs = FatFilesystem(PartitionView(source, p.start, p.size))
                top = {e.path.split("/")[1] for e in fs.scan(include_live=True) if e.path.lower().startswith("efi/") and "/" in e.path[4:]}
                out["efi_entries"] += sorted(top)
            except (FsError, OSError):
                continue
    low = {e.lower() for e in out["efi_entries"]}
    out["dual_boot"] = "microsoft" in low and bool(low - {"microsoft", "boot"})
    return out


# ---------------------------------------------------------------- running a plan
def run_plan(plan: Plan, *, execute: bool = False, confirmation: str | None = None) -> list[str]:
    """Return the command lines; run them only with execute=True, the typed phrase, and the matching OS."""
    lines = [" ".join(s.command) for s in plan.steps]
    if not execute:
        return lines
    require_typed_confirmation("RUN REPAIR PLAN", confirmation)
    if (plan.os_family == "windows") != (sys.platform == "win32"):
        raise OSError(f"This is a {plan.os_family} repair plan and cannot run on this operating system.")
    audit("run-plan", plan.title, "started")
    for s in plan.steps:
        if s.command[0] == "diskpart":
            raise OSError("diskpart steps are interactive; run them by hand.")
        proc = subprocess.run(s.command, capture_output=True, text=True, check=False)
        if proc.returncode:
            audit("run-plan", plan.title, f"failed at: {' '.join(s.command)}")
            raise OSError(f"step failed ({proc.returncode}): {' '.join(s.command)}\n{proc.stderr.strip()}")
    audit("run-plan", plan.title, "completed")
    return lines

