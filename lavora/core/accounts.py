"""Owner-only account access. Ownership attestation, audit log, no encryption bypass, ever.

Implemented: the attestation gate, the audit log, and Linux password reset inside a mounted root (via chpasswd in a
chroot). NOT implemented: offline reset of a local Windows account (needs a SAM/SYSTEM hive parser); calling it raises
NotImplementedError after the gate has run, so the audit trail and refusal rules are already in place for it.
"""
from __future__ import annotations

import getpass
import json
import subprocess
import sys
import time

from .paths import audit_log_path
from .safety import SafetyError

ATTESTATION = "this is my device"


class AccountRefused(SafetyError):
    pass


def audit(action: str, target: str, result: str, **extra: object) -> None:
    rec = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "os_user": getpass.getuser(), "action": action,
           "target": target, "result": result, **extra}
    with open(audit_log_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def require_attestation(statement: str | None, *, action: str, target: str) -> None:
    if (statement or "").strip().lower() != ATTESTATION:
        audit(action, target, "refused: no ownership attestation")
        raise AccountRefused(f'Type "{ATTESTATION}" to confirm you own this device and may access its accounts.')


def refuse_non_local(*, microsoft_account: bool = False, domain_account: bool = False, action: str, target: str) -> None:
    if microsoft_account or domain_account:
        audit(action, target, "refused: not a local account")
        raise AccountRefused("Microsoft and domain accounts cannot be reset offline. Use the provider's recovery "
                             "(account.live.com/password/reset) or your IT administrator.")


def reset_linux_password(root_mount: str, username: str, new_password: str, *, attestation: str | None,
                         dry_run: bool = True) -> list[str]:
    """chroot into the mounted root filesystem and run chpasswd. Returns the command that is (or would be) run."""
    require_attestation(attestation, action="linux-password-reset", target=f"{root_mount}:{username}")
    if not username.replace("_", "").replace("-", "").isalnum():
        raise AccountRefused("Unusual user name; refusing.")
    cmd = ["chroot", root_mount, "chpasswd"]
    if dry_run:
        audit("linux-password-reset", f"{root_mount}:{username}", "dry-run")
        return cmd
    if sys.platform == "win32":
        raise OSError("chroot is only available on Linux (use the Lavora Rescue environment)")
    proc = subprocess.run(cmd, input=f"{username}:{new_password}\n", text=True, capture_output=True, check=False)
    audit("linux-password-reset", f"{root_mount}:{username}", "ok" if proc.returncode == 0 else f"failed: {proc.returncode}")
    if proc.returncode:
        raise OSError(proc.stderr.strip() or "chpasswd failed")
    return cmd


def reset_windows_local_password(*, windows_dir: str, username: str, attestation: str | None,
                                 microsoft_account: bool = False, domain_account: bool = False) -> None:
    target = f"{windows_dir}:{username}"
    require_attestation(attestation, action="windows-password-reset", target=target)
    refuse_non_local(microsoft_account=microsoft_account, domain_account=domain_account,
                     action="windows-password-reset", target=target)
    audit("windows-password-reset", target, "not implemented")
    raise NotImplementedError("Offline Windows local-account reset is not implemented yet (needs a registry hive "
                              "parser). BitLocker-protected volumes can never be handled this way.")
