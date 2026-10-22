"""Disk editor: look at sectors, search for bytes, decode well-known structures, and (carefully) write sectors.

Viewing is read-only on any source. Writing goes through undo.write_regions, so it is sector-aligned, dry-run by default and
always preceded by an undo snapshot; writing to a physical disk additionally needs a typed confirmation in the caller.
"""
from __future__ import annotations

import struct
from collections.abc import Callable, Iterator

from .device import SECTOR, BlockSource
from .undo import WriteResult, write_regions


def hexview(src: BlockSource, offset: int, length: int = 512, width: int = 16) -> str:
    data = src.read_at(offset, length)
    lines = []
    for i in range(0, len(data), width):
        chunk = data[i:i + width]
        hexes = " ".join(f"{b:02x}" for b in chunk[:8]) + "  " + " ".join(f"{b:02x}" for b in chunk[8:])
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{offset + i:012x}  {hexes.ljust(width * 3 + 1)} |{text}|")
    return "\n".join(lines)


def find(src: BlockSource, pattern: bytes, *, start: int = 0, end: int | None = None, chunk: int = 4 * 1024 * 1024,
         cancel: Callable[[], bool] | None = None, progress=None, limit: int | None = None) -> Iterator[int]:
    """Yield every offset where `pattern` occurs (streaming, handles matches that straddle chunk boundaries)."""
    if not pattern:
        raise ValueError("empty pattern")
    stop = min(end if end is not None else src.size, src.size)
    pos = start
    found = 0
    keep = len(pattern) - 1
    while pos < stop:
        if cancel and cancel():
            return
        data = src.read_at(pos, min(chunk + keep, stop - pos))
        if not data:
            return
        i = data.find(pattern)
        while i != -1:
            if i < chunk or pos + chunk >= stop:  # matches that start in the overlap are reported by the next chunk
                yield pos + i
                found += 1
                if limit and found >= limit:
                    return
            i = data.find(pattern, i + 1)
        pos += chunk
        if progress:
            progress(min(pos, stop), stop)


def describe(sector: bytes) -> dict:
    """Name the structure in a 512-byte sector and decode its main fields (best effort, for the editor's side panel)."""
    out: dict = {"type": "unknown"}
    if len(sector) < SECTOR:
        return out
    if sector[3:11] == b"NTFS    ":
        bps, spc = struct.unpack_from("<HB", sector, 11)
        out = {"type": "NTFS boot sector", "bytes_per_sector": bps, "sectors_per_cluster": spc,
               "total_sectors": struct.unpack_from("<Q", sector, 40)[0], "mft_cluster": struct.unpack_from("<Q", sector, 48)[0],
               "serial": f"{struct.unpack_from('<Q', sector, 72)[0]:016X}"}
    elif sector[3:11] == b"EXFAT   ":
        out = {"type": "exFAT boot sector", "volume_length_sectors": struct.unpack_from("<Q", sector, 72)[0],
               "bytes_per_sector": 1 << sector[108], "sectors_per_cluster": 1 << sector[109]}
    elif sector[82:90] == b"FAT32   ":
        bps, spc, reserved, nfats = struct.unpack_from("<HBHB", sector, 11)
        out = {"type": "FAT32 boot sector", "bytes_per_sector": bps, "sectors_per_cluster": spc, "reserved_sectors": reserved,
               "fats": nfats, "fat_sectors": struct.unpack_from("<I", sector, 36)[0],
               "root_cluster": struct.unpack_from("<I", sector, 44)[0]}
    elif sector[:8] == b"EFI PART":
        out = {"type": "GPT header", "my_lba": struct.unpack_from("<Q", sector, 24)[0], "alternate_lba": struct.unpack_from("<Q", sector, 32)[0],
               "entries": struct.unpack_from("<I", sector, 80)[0], "entry_size": struct.unpack_from("<I", sector, 84)[0]}
    elif sector[510:512] == b"\x55\xaa":
        parts = []
        for i in range(4):
            e = sector[446 + 16 * i:462 + 16 * i]
            if e[4]:
                parts.append({"slot": i + 1, "type": f"0x{e[4]:02X}", "bootable": e[0] == 0x80,
                              "start_lba": struct.unpack_from("<I", e, 8)[0], "sectors": struct.unpack_from("<I", e, 12)[0]})
        out = {"type": "MBR / boot sector (55AA)", "partitions": parts}
    return out


def write_sector(target: str, lba: int, data: bytes, *, dry_run: bool = True, store: str | None = None) -> WriteResult:
    """Replace one or more whole sectors. Dry-run by default; real writes are snapshotted first and can be undone."""
    return write_regions(target, [(lba * SECTOR, data)], f"disk editor: write {len(data) // SECTOR} sector(s) at LBA {lba}",
                         dry_run=dry_run, store=store)
