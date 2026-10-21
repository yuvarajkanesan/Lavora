"""Safety rails shared by every feature that writes: output-volume checks and typed confirmations."""
from __future__ import annotations

import os

from .device import BlockSource, ImageSource, PartitionView


class SafetyError(Exception):
    """Raised when an operation would break one of Lavora's non-negotiable rules."""


def _root_source(source: BlockSource) -> BlockSource:
    while isinstance(source, PartitionView):
        source = source.parent
    return source


def _mountpoint_of(path: str) -> str:
    """Drive letter (Windows) or the longest matching mount point (Linux) that holds `path`."""
    path = os.path.abspath(path)
    drive = os.path.splitdrive(path)[0]
    if drive:
        return drive.upper()
    best = "/"
    p = path
    while p != "/":
        if os.path.ismount(p):
            best = p
            break
        p = os.path.dirname(p)
    return best


def check_output_location(source: BlockSource, out_path: str, *, disk_lookup=None) -> None:
    """Refuse to write recovered data onto the source.

    * Image sources: the output must not be the image itself. (The image is already a copy, so another folder on the
      same volume is allowed.)
    * Physical devices: the output must live on a volume that is NOT on the source disk. If we cannot tell which
      volumes belong to the disk, we refuse (fail closed).
    """
    root = _root_source(source)
    out_abs = os.path.abspath(out_path)
    if isinstance(root, ImageSource):
        if os.path.normcase(out_abs) == os.path.normcase(root.path):
            raise SafetyError("Output path is the source image itself.")
        return
    if not root.is_device:
        return
    if disk_lookup is None:
        from .disks import disk_for_path as disk_lookup
    disk = disk_lookup(root.path)
    if disk is None:
        raise SafetyError(f"Cannot determine which volumes live on {root.path}; refusing to write (fail closed).")
    if _mountpoint_of(out_abs).rstrip("\\/").upper() in disk.mountpoints():
        raise SafetyError(
            f"{out_abs} is on the disk being recovered ({root.path}). Choose a folder on a different disk or volume.")


def require_typed_confirmation(expected: str, given: str | None) -> None:
    """Destructive actions need the user to type an exact phrase (case-insensitive)."""
    if given is None or given.strip().lower() != expected.strip().lower():
        raise SafetyError(f'Confirmation required: type "{expected}" to continue.')
