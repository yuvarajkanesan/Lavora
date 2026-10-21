"""Unlock BitLocker / LUKS volumes with the user's own password or key. There is no bypass and never will be.

These are thin wrappers around the vendor tools (manage-bde on Windows, cryptsetup on Linux). The secret is passed
on stdin or by key file, never on a command line, and is never logged.
"""
from __future__ import annotations

import shutil
import subprocess
import sys


def bitlocker_unlock_command(drive: str, *, recovery_password: bool) -> list[str]:
    """Command that unlocks `drive` (e.g. 'D:'); the password is typed at the manage-bde prompt, not passed here."""
    flag = "-RecoveryPassword" if recovery_password else "-Password"
    return ["manage-bde", "-unlock", drive, flag]


def luks_open_command(device: str, name: str, key_file: str | None = None) -> list[str]:
    cmd = ["cryptsetup", "open", device, name]
    if key_file:
        cmd += ["--key-file", key_file]
    return cmd


def luks_open(device: str, name: str, password: str) -> None:
    if sys.platform == "win32" or not shutil.which("cryptsetup"):
        raise OSError("cryptsetup is not available here (use Lavora Rescue / Linux)")
    proc = subprocess.run(luks_open_command(device, name, "-"), input=password, text=True, capture_output=True,
                          check=False)
    if proc.returncode:
        raise OSError("Could not unlock: wrong password, or not a LUKS volume.")
