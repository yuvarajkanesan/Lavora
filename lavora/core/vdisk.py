"""Virtual disk containers as read-only block sources: dynamic VHD, VHDX, VMDK (sparse and flat) and VDI.

Open any of them with device.open_source(path); the result is an ordinary BlockSource, so scanning, recovery, carving,
imaging and RAID work on virtual machines' disks exactly as on physical ones. Unallocated blocks read as zeros.
Not supported (reported clearly, never guessed): differencing/snapshot chains (they need their parent), compressed VMDK
grains, encrypted containers.
"""
from __future__ import annotations

import os
import re
import struct
import uuid
from typing import BinaryIO

from .device import BlockSource, ImageSource


class VirtualDiskError(Exception):
    pass


class _Blocked(BlockSource):
    """Shared plumbing: a disk made of fixed-size blocks that live somewhere in a container file (or nowhere = zeros)."""

    block_size = 0

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self._fh: BinaryIO = open(self.path, "rb")  # noqa: SIM115 - closed in close()

    def _locate(self, block: int) -> int | None:
        """File offset of the start of `block`'s data, or None if it is unallocated (zeros)."""
        raise NotImplementedError

    def read_at(self, offset: int, length: int) -> bytes:
        if offset < 0 or offset >= self.size or length <= 0:
            return b""
        length = min(length, self.size - offset)
        out = bytearray()
        pos = offset
        while len(out) < length:
            blk, inner = divmod(pos, self.block_size)
            take = min(self.block_size - inner, length - len(out))
            where = self._locate(blk)
            if where is None:
                out += bytes(take)
            else:
                self._fh.seek(where + inner)
                data = self._fh.read(take)
                out += data.ljust(take, b"\0")
            pos += take
        return bytes(out)

    def close(self) -> None:
        self._fh.close()


class VhdDynamic(_Blocked):
    def __init__(self, path: str):
        super().__init__(path)
        f = self._fh
        footer = _read(f, 0, 512)
        if footer[:8] != b"conectix":
            raise VirtualDiskError("not a VHD")
        dtype = struct.unpack(">I", footer[60:64])[0]
        if dtype == 4:
            raise VirtualDiskError("this is a differencing VHD: it needs its parent disk (open the parent chain with Hyper-V first)")
        if dtype != 3:
            raise VirtualDiskError(f"unsupported VHD type {dtype}")
        self.size = struct.unpack(">Q", footer[48:56])[0]
        hdr_off = struct.unpack(">Q", footer[16:24])[0]
        hdr = _read(f, hdr_off, 1024)
        if hdr[:8] != b"cxsparse":
            raise VirtualDiskError("dynamic VHD header missing")
        table_off = struct.unpack(">Q", hdr[16:24])[0]
        entries = struct.unpack(">I", hdr[28:32])[0]
        self.block_size = struct.unpack(">I", hdr[32:36])[0]
        self._bat = struct.unpack(f">{entries}I", _read(f, table_off, entries * 4))
        sectors = self.block_size // 512
        self._bitmap = ((sectors + 7) // 8 + 511) // 512 * 512  # sector bitmap precedes each block's data

    def _locate(self, block: int) -> int | None:
        if block >= len(self._bat) or self._bat[block] == 0xFFFFFFFF:
            return None
        return self._bat[block] * 512 + self._bitmap


class Vdi(_Blocked):
    def __init__(self, path: str):
        super().__init__(path)
        h = _read(self._fh, 0, 512)
        if struct.unpack_from("<I", h, 64)[0] != 0xBEDA107F:
            raise VirtualDiskError("not a VDI")
        off_blocks, off_data = struct.unpack_from("<II", h, 340)
        self.size = struct.unpack_from("<Q", h, 368)[0]
        self.block_size, _extra, n_blocks = struct.unpack_from("<III", h, 376)
        self._off_data = off_data
        self._map = struct.unpack(f"<{n_blocks}I", _read(self._fh, off_blocks, n_blocks * 4))

    def _locate(self, block: int) -> int | None:
        if block >= len(self._map) or self._map[block] >= 0xFFFFFFFE:  # free / zero
            return None
        return self._off_data + self._map[block] * self.block_size


class VmdkSparse(_Blocked):
    def __init__(self, path: str):
        super().__init__(path)
        h = _read(self._fh, 0, 512)
        if h[:4] != b"KDMV":
            raise VirtualDiskError("not a VMDK")
        flags = struct.unpack_from("<I", h, 8)[0]
        if flags & (1 << 16):
            raise VirtualDiskError("this VMDK stores compressed grains (streamOptimized); convert it with vmware-vdiskmanager first")
        capacity, grain = struct.unpack_from("<QQ", h, 12)
        self.size = capacity * 512
        self.block_size = grain * 512
        self._gtes = struct.unpack_from("<I", h, 44)[0]
        gd_off = struct.unpack_from("<Q", h, 56)[0]
        n_gt = (capacity + grain * self._gtes - 1) // (grain * self._gtes)
        self._gd = struct.unpack(f"<{n_gt}I", _read(self._fh, gd_off * 512, n_gt * 4))
        self._gt_cache: dict[int, tuple[int, ...]] = {}

    def _locate(self, block: int) -> int | None:
        gt_i, idx = divmod(block, self._gtes)
        if gt_i >= len(self._gd) or self._gd[gt_i] == 0:
            return None
        gt = self._gt_cache.get(gt_i)
        if gt is None:
            if len(self._gt_cache) > 256:
                self._gt_cache.clear()
            gt = self._gt_cache[gt_i] = struct.unpack(f"<{self._gtes}I", _read(self._fh, self._gd[gt_i] * 512, self._gtes * 4))
        sector = gt[idx]
        return None if sector in (0, 1) else sector * 512  # 0 = unallocated, 1 = zero grain


_VHDX_BAT = uuid.UUID("2DC27766-F623-4200-9D64-115E9BFD4A08").bytes_le
_VHDX_META = uuid.UUID("8B7CA206-4790-4B9A-B8FE-575F3FB84C98").bytes_le
_VHDX_PARAMS = uuid.UUID("CAA16737-FA36-4D43-B3B6-33F0AA44E76B").bytes_le
_VHDX_SIZE = uuid.UUID("2FA54224-CD1B-4876-B211-5DBED83BF4B8").bytes_le
_VHDX_LSECT = uuid.UUID("8141BF1D-A96F-4709-BA47-F233A8FAAB5F").bytes_le


class Vhdx(_Blocked):
    def __init__(self, path: str):
        super().__init__(path)
        f = self._fh
        if _read(f, 0, 8) != b"vhdxfile":
            raise VirtualDiskError("not a VHDX")
        rt = _read(f, 192 * 1024, 64 * 1024)
        if rt[:4] != b"regi":
            raise VirtualDiskError("VHDX region table missing")
        regions = {}
        for i in range(struct.unpack_from("<I", rt, 8)[0]):
            e = rt[16 + 32 * i:48 + 32 * i]
            regions[e[:16]] = struct.unpack_from("<QI", e, 16)
        if _VHDX_BAT not in regions or _VHDX_META not in regions:
            raise VirtualDiskError("VHDX BAT/metadata region missing")
        m_off, m_len = regions[_VHDX_META]
        meta = _read(f, m_off, m_len)
        items = {}
        for i in range(struct.unpack_from("<H", meta, 10)[0]):
            e = meta[32 + 32 * i:64 + 32 * i]
            off, ln = struct.unpack_from("<II", e, 16)
            items[e[:16]] = meta[off:off + ln]
        self.block_size, vflags = struct.unpack_from("<II", items[_VHDX_PARAMS], 0)
        if vflags & 2:
            raise VirtualDiskError("this is a differencing VHDX: it needs its parent disk")
        self.size = struct.unpack_from("<Q", items[_VHDX_SIZE], 0)[0]
        lsect = struct.unpack_from("<I", items[_VHDX_LSECT], 0)[0]
        self._ratio = (1 << 23) * lsect // self.block_size  # payload blocks per sector-bitmap entry
        b_off, b_len = regions[_VHDX_BAT]
        self._bat = struct.unpack(f"<{b_len // 8}Q", _read(f, b_off, b_len // 8 * 8))

    def _locate(self, block: int) -> int | None:
        i = block + block // self._ratio
        if i >= len(self._bat):
            return None
        entry = self._bat[i]
        if entry & 7 not in (6, 7):  # not present / zero / unmapped
            return None
        return (entry >> 20) << 20


def _read(f: BinaryIO, off: int, n: int) -> bytes:
    f.seek(off)
    data = f.read(n)
    if len(data) < n:
        raise VirtualDiskError("virtual disk file is truncated")
    return data


class VmdkFlat(BlockSource):
    """A text VMDK descriptor pointing at one flat extent file (the common 'monolithicFlat' layout)."""

    def __init__(self, descriptor: str):
        with open(descriptor, encoding="utf-8", errors="replace") as f:
            text = f.read()
        extents = re.findall(r'^(?:RW|RDONLY)\s+(\d+)\s+FLAT\s+"([^"]+)"\s+(\d+)', text, re.M)
        if len(extents) != 1:
            raise VirtualDiskError("only single-extent flat VMDK descriptors are supported")
        sectors, name, offset = extents[0]
        self._inner = ImageSource(os.path.join(os.path.dirname(os.path.abspath(descriptor)), name))
        self._off = int(offset) * 512
        self.size = int(sectors) * 512
        self.path = os.path.abspath(descriptor)

    def read_at(self, offset: int, length: int) -> bytes:
        if offset < 0 or offset >= self.size:
            return b""
        return self._inner.read_at(self._off + offset, min(length, self.size - offset))

    def close(self) -> None:
        self._inner.close()


def open_virtual(path: str) -> BlockSource | None:
    """Return a virtual-disk source if `path` is one of the supported containers, else None (plain image)."""
    try:
        with open(path, "rb") as f:
            head = f.read(512)
    except OSError:
        return None
    if head[:8] == b"conectix" and len(head) >= 64 and struct.unpack(">I", head[60:64])[0] in (3, 4):
        return VhdDynamic(path)
    if head[:8] == b"vhdxfile":
        return Vhdx(path)
    if head[:4] == b"KDMV":
        return VmdkSparse(path)
    if len(head) >= 68 and struct.unpack_from("<I", head, 64)[0] == 0xBEDA107F:
        return Vdi(path)
    if head.startswith(b"# Disk DescriptorFile"):
        return VmdkFlat(path)
    return None

