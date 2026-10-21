"""Filesystem parsers. open_filesystem() picks one by looking at the volume."""
from __future__ import annotations

from ..device import BlockSource
from ..partitions import identify_fs
from .base import FileEntry, Filesystem, FsError, UnsupportedError
from .ext import ExtFilesystem
from .fat import ExfatFilesystem, FatFilesystem
from .ntfs import NtfsFilesystem

__all__ = ["FileEntry", "Filesystem", "FsError", "UnsupportedError", "open_filesystem"]


def open_filesystem(source: BlockSource) -> Filesystem:
    kind = identify_fs(source, 0)
    if kind == "ntfs":
        return NtfsFilesystem(source)
    if kind == "exfat":
        return ExfatFilesystem(source)
    if kind in ("fat32", "fat16", "fat12"):
        return FatFilesystem(source)
    if kind == "ext":
        return ExtFilesystem(source)
    raise FsError("unrecognised filesystem (try a signature carve or a lost-partition scan)")
