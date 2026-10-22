"""Read-only block sources: image files, physical devices, and partition slices.

Nothing in this module can write. Sources are always opened 'rb'; the only write paths in Lavora live in
lavora.core.undo / repair and operate on explicit, snapshotted flows.
"""
from __future__ import annotations

import os
import struct
import sys
from abc import ABC, abstractmethod
from typing import BinaryIO

SECTOR = 512


class BlockSource(ABC):
    """A readable run of bytes with a known size."""

    size: int
    path: str
    is_device = False

    @abstractmethod
    def read_at(self, offset: int, length: int) -> bytes:
        """Return up to `length` bytes at `offset` (short only at end of source). Raises OSError on a bad sector."""

    def close(self) -> None:  # noqa: B027 - optional hook; in-memory sources have nothing to release
        pass

    def __enter__(self) -> BlockSource:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def read_exact(self, offset: int, length: int) -> bytes:
        data = self.read_at(offset, length)
        if len(data) != length:
            raise OSError(f"short read at {offset}: wanted {length}, got {len(data)}")
        return data


class ImageSource(BlockSource):
    """A raw image file (.img/.dd/.raw/.iso) or a fixed-size VHD (raw data followed by a 512-byte footer)."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self._fh: BinaryIO = open(self.path, "rb")  # noqa: SIM115 - closed in close()
        self._fh.seek(0, os.SEEK_END)
        self.size = self._fh.tell()
        self._strip_vhd_footer()

    def _strip_vhd_footer(self) -> None:
        if self.size < SECTOR or not self.path.lower().endswith(".vhd"):
            return
        self._fh.seek(self.size - SECTOR)
        footer = self._fh.read(SECTOR)
        # disk type 2 == fixed: the data is plain sectors, so the footer is simply not part of the disk
        if footer[:8] == b"conectix" and struct.unpack(">I", footer[60:64])[0] == 2:
            self.size -= SECTOR

    def read_at(self, offset: int, length: int) -> bytes:
        if offset < 0 or offset >= self.size or length <= 0:
            return b""
        length = min(length, self.size - offset)
        self._fh.seek(offset)
        return self._fh.read(length)

    def close(self) -> None:
        self._fh.close()


class DeviceSource(BlockSource):
    """A physical disk (\\\\.\\PhysicalDriveN or /dev/sdX). Reads are sector-aligned internally; opened read-only."""

    is_device = True

    def __init__(self, path: str):
        self.path = path
        self._fh: BinaryIO = open(path, "rb", buffering=0)  # noqa: SIM115 - closed in close()
        self.size = _device_size(self._fh, path)

    def read_at(self, offset: int, length: int) -> bytes:
        if offset < 0 or offset >= self.size or length <= 0:
            return b""
        length = min(length, self.size - offset)
        start = offset - offset % SECTOR
        end = offset + length
        end += -end % SECTOR
        self._fh.seek(start)
        raw = self._fh.read(end - start)
        skip = offset - start
        return raw[skip:skip + length]

    def close(self) -> None:
        self._fh.close()


class PartitionView(BlockSource):
    """A byte range of another source, presented as its own source starting at 0."""

    def __init__(self, parent: BlockSource, offset: int, length: int):
        self.parent = parent
        self.offset = offset
        self.size = max(0, min(length, parent.size - offset))
        self.path = f"{parent.path}@{offset}"
        self.is_device = parent.is_device

    def read_at(self, offset: int, length: int) -> bytes:
        if offset < 0 or offset >= self.size or length <= 0:
            return b""
        return self.parent.read_at(self.offset + offset, min(length, self.size - offset))


class BytesSource(BlockSource):
    """In-memory source (tests and small tools)."""

    def __init__(self, data: bytes, path: str = "<memory>"):
        self._data = data
        self.size = len(data)
        self.path = path

    def read_at(self, offset: int, length: int) -> bytes:
        if offset < 0 or length <= 0:
            return b""
        return self._data[offset:offset + length]


def _device_size(fh: BinaryIO, path: str) -> int:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C
        import msvcrt

        handle = msvcrt.get_osfhandle(fh.fileno())
        out = ctypes.c_longlong(0)
        returned = wintypes.DWORD(0)
        ok = ctypes.windll.kernel32.DeviceIoControl(  # type: ignore[attr-defined]
            wintypes.HANDLE(handle), IOCTL_DISK_GET_LENGTH_INFO, None, 0,
            ctypes.byref(out), ctypes.sizeof(out), ctypes.byref(returned), None)
        if not ok:
            raise OSError(f"cannot determine size of {path}")
        return int(out.value)
    return fh.seek(0, os.SEEK_END)


def open_source(path: str) -> BlockSource:
    """Open a path as a read-only source: devices by name, everything else as an image file."""
    low = path.lower()
    if low.startswith("raid:"):  # a virtual RAID described by a spec string (see lavora.core.raid)
        from .raid import open_raid_spec
        return open_raid_spec(path)
    if low.startswith("\\\\.\\physicaldrive") or low.startswith("/dev/"):
        return DeviceSource(path)
    return ImageSource(path)
