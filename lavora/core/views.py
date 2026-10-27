"""Read-only views that patch a source in memory: used to open a filesystem through its BACKUP metadata when the primary copy is
damaged, without ever writing to the disk or image."""
from __future__ import annotations

from .device import SECTOR, BlockSource


class OverlayView(BlockSource):
    """`parent` with some sector-aligned regions replaced by other bytes (e.g. the NTFS backup boot sector shown as sector 0)."""

    def __init__(self, parent: BlockSource, replacements: dict[int, bytes], label: str = ""):
        for off, data in replacements.items():
            if off % SECTOR or len(data) % SECTOR:
                raise ValueError("replacements must be sector aligned")
        self.parent, self.repl, self.label = parent, dict(replacements), label
        self.size, self.path, self.is_device = parent.size, f"{parent.path}#{label or 'overlay'}", parent.is_device

    def read_at(self, offset: int, length: int) -> bytes:
        data = bytearray(self.parent.read_at(offset, length))
        for off, rep in self.repl.items():
            lo, hi = max(off, offset), min(off + len(rep), offset + len(data))
            if lo < hi:
                data[lo - offset:hi - offset] = rep[lo - off:hi - off]
        return bytes(data)

    def close(self) -> None:  # the parent is owned by the caller
        pass
