"""Common types for the filesystem parsers."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import BinaryIO

from ..device import BlockSource


class UnsupportedError(Exception):
    """The file uses a feature we cannot read yet (compression, encryption, attribute lists...)."""


class FsError(Exception):
    """The filesystem structures are damaged or not what we expected."""


@dataclass
class FileEntry:
    path: str  # forward-slash path from the volume root
    name: str
    size: int
    is_dir: bool = False
    deleted: bool = False
    mtime: datetime | None = None
    ctime: datetime | None = None
    ref: int = 0  # MFT record / inode / first cluster: filesystem-specific handle
    fs: str = ""
    overwritten_pct: float = 0.0  # share of the file's clusters that are allocated to something else now
    note: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def extension(self) -> str:
        return self.name.rsplit(".", 1)[-1].lower() if "." in self.name else ""

    @property
    def recoverable(self) -> bool:
        return not self.is_dir and not self.note.startswith("unsupported") and self.overwritten_pct < 100.0

    def to_dict(self) -> dict:
        return {"path": self.path, "size": self.size, "deleted": self.deleted, "is_dir": self.is_dir,
                "mtime": self.mtime.isoformat() if self.mtime else None, "fs": self.fs, "ref": self.ref,
                "overwritten_pct": round(self.overwritten_pct, 1), "note": self.note}


class Filesystem(ABC):
    name = ""

    def __init__(self, source: BlockSource):
        self.source = source

    @abstractmethod
    def scan(self, *, include_live: bool = True, progress=None) -> Iterator[FileEntry]:
        """Yield files and directories (deleted ones flagged)."""

    @abstractmethod
    def read(self, entry: FileEntry, offset: int = 0, length: int | None = None) -> bytes:
        """Read content of `entry`."""

    def copy_to(self, entry: FileEntry, out: BinaryIO, *, block: int = 1 << 20) -> int:
        """Stream the whole file to `out`; returns bytes written."""
        done = 0
        while done < entry.size:
            data = self.read(entry, done, min(block, entry.size - done))
            if not data:
                break
            out.write(data)
            done += len(data)
        return done


def filetime_to_dt(ft: int) -> datetime | None:
    if not ft:
        return None
    try:
        return datetime(1601, 1, 1, tzinfo=UTC) + timedelta(microseconds=ft // 10)
    except OverflowError:
        return None


def dos_to_dt(date: int, time_: int, tenths: int = 0) -> datetime | None:
    try:
        return datetime(1980 + (date >> 9), (date >> 5) & 15, date & 31, time_ >> 11, (time_ >> 5) & 63,
                        (time_ & 31) * 2, tzinfo=UTC)
    except ValueError:
        return None
