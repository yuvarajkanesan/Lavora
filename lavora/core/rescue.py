"""Lavora Rescue USB: write a Rescue ISO to a USB stick, with the safety checks a destructive operation deserves.

The ISO itself is built by rescue/build-iso.sh (Debian live-build, run in WSL2/Docker); this module only writes it.
"""
from __future__ import annotations

import os

from .device import ImageSource
from .disks import DiskInfo, list_disks
from .imaging import ImageResult, clone, verify_copy
from .safety import SafetyError, require_typed_confirmation


def check_usb_target(disk: DiskInfo, iso_size: int) -> None:
    """Only a removable, non-system disk big enough for the ISO may be erased."""
    if disk.is_system:
        raise SafetyError(f"{disk.path} holds the running system; refusing to erase it.")
    if not disk.removable:
        raise SafetyError(f"{disk.path} is not a removable (USB) disk; refusing to erase it.")
    if disk.size < iso_size:
        raise SafetyError(f"{disk.path} is smaller than the ISO ({disk.size} < {iso_size} bytes).")


def write_iso_to_usb(iso_path: str, device: str, *, confirmation: str | None, disk_lookup=None, verify: bool = True,
                     progress=None) -> ImageResult:
    if not os.path.isfile(iso_path):
        raise SafetyError(f"ISO not found: {iso_path} (build it with rescue/build-iso.sh)")
    disks = (disk_lookup or list_disks)()
    disk = next((d for d in disks if d.path.lower() == device.lower()), None)
    if disk is None:
        raise SafetyError(f"{device} is not a known disk; refusing (fail closed).")
    check_usb_target(disk, os.path.getsize(iso_path))
    require_typed_confirmation(f"ERASE {device}", confirmation)
    with ImageSource(iso_path) as iso:
        res = clone(iso, device, confirmation=confirmation, progress=progress)
        if verify:
            from .device import open_source
            with open_source(device) as usb:
                res.complete = res.complete and verify_copy(iso, _Prefix(usb, iso.size))
    return res


class _Prefix(ImageSource):
    """First `size` bytes of a larger device, so a verify compares like with like."""

    def __init__(self, inner, size: int):  # noqa: D107 - tiny adapter
        self._inner, self.size, self.path = inner, size, inner.path

    def read_at(self, offset: int, length: int) -> bytes:
        return self._inner.read_at(offset, min(length, self.size - offset)) if offset < self.size else b""

    def close(self) -> None:
        pass
