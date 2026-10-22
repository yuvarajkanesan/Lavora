"""MBR / GPT parsing, filesystem identification, lost-partition scanning and partition-table rebuilding."""
from __future__ import annotations

import struct
import uuid
import zlib
from dataclasses import dataclass, field

from .device import SECTOR, BlockSource

MBR_TYPES = {0x01: "fat12", 0x04: "fat16", 0x06: "fat16", 0x0B: "fat32", 0x0C: "fat32", 0x07: "ntfs/exfat",
             0x83: "ext", 0xEE: "gpt-protective", 0x05: "extended", 0x0F: "extended"}
GPT_BASIC_DATA = "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7"
GPT_LINUX_DATA = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"
GPT_ESP = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"


@dataclass
class Partition:
    index: int
    start: int  # bytes
    size: int  # bytes
    scheme: str  # MBR / GPT / found
    type_desc: str = ""
    fs: str = ""
    bootable: bool = False
    name: str = ""
    note: str = ""


@dataclass
class GptInfo:
    primary_ok: bool = False
    backup_ok: bool = False
    primary_error: str = ""
    backup_error: str = ""
    disk_guid: str = ""
    entries: list[Partition] = field(default_factory=list)
    entries_raw: bytes = b""
    header: dict = field(default_factory=dict)


# ---------------------------------------------------------------- filesystem identification
def identify_fs(source: BlockSource, offset: int) -> str:
    """Name the filesystem whose boot sector / superblock starts at `offset` ('' if unknown)."""
    boot = source.read_at(offset, SECTOR)
    if len(boot) < SECTOR:
        return ""
    if boot[3:11] == b"NTFS    ":
        return "ntfs"
    if boot[3:11] == b"EXFAT   ":
        return "exfat"
    if boot[82:90] == b"FAT32   ":
        return "fat32"
    if boot[510:512] == b"\x55\xaa" and boot[54:57] == b"FAT":
        return "fat16" if boot[54:62] != b"FAT12   " else "fat12"
    sb = source.read_at(offset + 1024, 1024)
    if len(sb) >= 60 and struct.unpack_from("<H", sb, 56)[0] == 0xEF53:
        return "ext"
    return ""


# ---------------------------------------------------------------- MBR
def parse_mbr(sector: bytes) -> list[Partition]:
    if len(sector) < SECTOR or sector[510:512] != b"\x55\xaa":
        return []
    parts = []
    for i in range(4):
        e = sector[446 + 16 * i:462 + 16 * i]
        boot, ptype = e[0], e[4]
        lba, count = struct.unpack_from("<II", e, 8)
        if ptype == 0 or count == 0:
            continue
        parts.append(Partition(i + 1, lba * SECTOR, count * SECTOR, "MBR", MBR_TYPES.get(ptype, f"0x{ptype:02X}"),
                               bootable=boot == 0x80))
    return parts


def build_mbr(parts: list[Partition], *, boot_code: bytes | None = None, signature: int = 0) -> bytes:
    """Build a 512-byte MBR with up to four primary entries. Partitions must fit 32-bit LBA addressing."""
    if len(parts) > 4:
        raise ValueError("an MBR holds at most 4 primary partitions")
    sector = bytearray(boot_code[:446].ljust(446, b"\0") if boot_code else bytes(446))
    sector += bytes(64)
    struct.pack_into("<I", sector, 440, signature)
    for i, p in enumerate(parts):
        lba, count = p.start // SECTOR, p.size // SECTOR
        if lba >= 2 ** 32 or count >= 2 ** 32:
            raise ValueError("partition does not fit an MBR (needs GPT)")
        ptype = {"ntfs": 0x07, "exfat": 0x07, "fat32": 0x0C, "fat16": 0x06, "fat12": 0x01, "ext": 0x83}.get(p.fs, 0x07)
        # CHS fields are unused by modern firmware; 0xFE/0xFF/0xFF means "use LBA"
        sector[446 + 16 * i:462 + 16 * i] = struct.pack("<B3sB3sII", 0x80 if p.bootable else 0, b"\xfe\xff\xff", ptype,
                                                        b"\xfe\xff\xff", lba, count)
    sector += b"\x55\xaa"
    return bytes(sector[:512])


# ---------------------------------------------------------------- GPT
def _parse_gpt_header(raw: bytes) -> dict | None:
    if len(raw) < 92 or raw[:8] != b"EFI PART":
        return None
    hsize = struct.unpack_from("<I", raw, 12)[0]
    if hsize < 92 or hsize > SECTOR:
        return None
    return {
        "revision": raw[8:12], "header_size": hsize, "crc": struct.unpack_from("<I", raw, 16)[0],
        "my_lba": struct.unpack_from("<Q", raw, 24)[0], "alt_lba": struct.unpack_from("<Q", raw, 32)[0],
        "first_usable": struct.unpack_from("<Q", raw, 40)[0], "last_usable": struct.unpack_from("<Q", raw, 48)[0],
        "disk_guid": raw[56:72], "entries_lba": struct.unpack_from("<Q", raw, 72)[0],
        "num_entries": struct.unpack_from("<I", raw, 80)[0], "entry_size": struct.unpack_from("<I", raw, 84)[0],
        "entries_crc": struct.unpack_from("<I", raw, 88)[0], "raw": raw[:hsize],
    }


def _check_gpt_copy(source: BlockSource, lba: int) -> tuple[dict | None, bytes, str]:
    raw = source.read_at(lba * SECTOR, SECTOR)
    h = _parse_gpt_header(raw)
    if h is None:
        return None, b"", "no GPT signature"
    hdr = bytearray(h["raw"])
    struct.pack_into("<I", hdr, 16, 0)
    if zlib.crc32(bytes(hdr)) & 0xFFFFFFFF != h["crc"]:
        return h, b"", "header CRC mismatch"
    if h["entry_size"] < 128 or h["num_entries"] > 4096:
        return h, b"", "implausible entry table geometry"
    entries = source.read_at(h["entries_lba"] * SECTOR, h["num_entries"] * h["entry_size"])
    if zlib.crc32(entries) & 0xFFFFFFFF != h["entries_crc"]:
        return h, entries, "partition entry array CRC mismatch"
    return h, entries, ""


def _guid(b: bytes) -> str:
    return str(uuid.UUID(bytes_le=b)).upper()


def parse_gpt(source: BlockSource) -> GptInfo:
    info = GptInfo()
    last_lba = source.size // SECTOR - 1
    ph, pe, perr = _check_gpt_copy(source, 1)
    bh, be, berr = _check_gpt_copy(source, last_lba)
    info.primary_ok, info.primary_error = ph is not None and not perr, perr
    info.backup_ok, info.backup_error = bh is not None and not berr, berr
    good_h, good_e = (ph, pe) if info.primary_ok else (bh, be) if info.backup_ok else (None, b"")
    if good_h is None:
        return info
    info.header, info.entries_raw, info.disk_guid = good_h, good_e, _guid(good_h["disk_guid"])
    size = good_h["entry_size"]
    for i in range(good_h["num_entries"]):
        e = good_e[i * size:(i + 1) * size]
        if len(e) < 128 or e[:16] == bytes(16):
            continue
        first, last = struct.unpack_from("<QQ", e, 32)
        tguid = _guid(e[:16])
        fs = {GPT_BASIC_DATA: "ntfs/fat/exfat", GPT_LINUX_DATA: "ext", GPT_ESP: "fat32"}.get(tguid, "")
        info.entries.append(Partition(i + 1, first * SECTOR, (last - first + 1) * SECTOR, "GPT", tguid, fs,
                                      name=e[56:128].decode("utf-16-le", "ignore").rstrip("\0")))
    return info


def build_gpt_pair(source_size: int, parts: list[Partition], disk_guid: uuid.UUID | None = None,
                   *, entries: int = 128) -> tuple[bytes, bytes, bytes]:
    """Return (primary_header_sector, entries_array, backup_header_sector) for a fresh GPT over `parts`."""
    total_lba = source_size // SECTOR
    esize = 128
    entry_sectors = entries * esize // SECTOR
    arr = bytearray(entries * esize)
    for i, p in enumerate(parts):
        tguid = {"ntfs": GPT_BASIC_DATA, "exfat": GPT_BASIC_DATA, "fat32": GPT_BASIC_DATA, "ext": GPT_LINUX_DATA}.get(
            p.fs, GPT_BASIC_DATA)
        first, last = p.start // SECTOR, (p.start + p.size) // SECTOR - 1
        name = p.name.encode("utf-16-le")[:72]
        arr[i * esize:(i + 1) * esize] = (uuid.UUID(tguid).bytes_le + uuid.uuid4().bytes_le +
                                           struct.pack("<QQQ", first, last, 0) + name.ljust(72, b"\0"))
    arr_crc = zlib.crc32(bytes(arr)) & 0xFFFFFFFF
    guid = (disk_guid or uuid.uuid4()).bytes_le

    def header(my: int, alt: int, entries_lba: int) -> bytes:
        h = bytearray(92)
        h[:8] = b"EFI PART"
        struct.pack_into("<IIII", h, 8, 0x00010000, 92, 0, 0)
        struct.pack_into("<QQQQ", h, 24, my, alt, 2 + entry_sectors, total_lba - 2 - entry_sectors)
        h[56:72] = guid
        struct.pack_into("<QIII", h, 72, entries_lba, entries, esize, arr_crc)
        struct.pack_into("<I", h, 16, zlib.crc32(bytes(h)) & 0xFFFFFFFF)
        return bytes(h).ljust(SECTOR, b"\0")

    return (header(1, total_lba - 1, 2), bytes(arr), header(total_lba - 1, 1, total_lba - 1 - entry_sectors))


def gpt_repair_regions(source: BlockSource) -> list[tuple[int, bytes]]:
    """Regions that restore the damaged GPT copy from the surviving one (empty if nothing to do / nothing to copy)."""
    info = parse_gpt(source)
    total_lba = source.size // SECTOR
    if info.primary_ok == info.backup_ok:
        return []
    h = info.header
    n_sect = h["num_entries"] * h["entry_size"] // SECTOR
    arr = info.entries_raw.ljust(n_sect * SECTOR, b"\0")
    guid_le = h["disk_guid"]
    arr_crc = h["entries_crc"]

    def header(my: int, alt: int, entries_lba: int) -> bytes:
        raw = bytearray(h["raw"].ljust(92, b"\0"))
        struct.pack_into("<I", raw, 16, 0)
        struct.pack_into("<QQ", raw, 24, my, alt)
        struct.pack_into("<Q", raw, 72, entries_lba)
        raw[56:72] = guid_le
        struct.pack_into("<I", raw, 88, arr_crc)
        struct.pack_into("<I", raw, 16, zlib.crc32(bytes(raw[:h["header_size"]])) & 0xFFFFFFFF)
        return bytes(raw).ljust(SECTOR, b"\0")

    if info.backup_ok:  # rebuild the primary at LBA 1, entries at LBA 2
        return [(SECTOR, header(1, total_lba - 1, 2)), (2 * SECTOR, arr)]
    return [((total_lba - 1) * SECTOR, header(total_lba - 1, 1, total_lba - 1 - n_sect)),
            ((total_lba - 1 - n_sect) * SECTOR, arr)]


# ---------------------------------------------------------------- lost partition scan
@dataclass
class FoundPartition:
    start: int
    size: int
    fs: str
    confidence: str  # high / medium / low
    note: str = ""


def _ntfs_geometry(boot: bytes) -> tuple[int, int, int, int] | None:
    bps, spc = struct.unpack_from("<HB", boot, 11)
    total = struct.unpack_from("<Q", boot, 40)[0]
    mft = struct.unpack_from("<Q", boot, 48)[0]
    if bps not in (512, 1024, 2048, 4096) or spc == 0 or spc & (spc - 1) and spc < 128 or total == 0:
        return None
    return bps, spc, total, mft


def _valid_boot(source: BlockSource, off: int, fs: str) -> FoundPartition | None:
    boot = source.read_at(off, SECTOR)
    if len(boot) < SECTOR or boot[510:512] != b"\x55\xaa":
        return None
    if fs == "ntfs":
        g = _ntfs_geometry(boot)
        if not g:
            return None
        bps, spc, total, mft = g
        size = (total + 1) * bps
        if source.read_at(off + mft * spc * bps, 4) == b"FILE":
            return FoundPartition(off, size, "ntfs", "high")
        implied = off - total * bps  # this may be the *backup* boot sector in the last sector of the volume
        if implied >= 0 and source.read_at(implied + mft * spc * bps, 4) == b"FILE":
            return FoundPartition(implied, size, "ntfs", "medium", "located through the backup boot sector")
        return FoundPartition(off, size, "ntfs", "low", "MFT not found at the expected place")
    if fs == "fat32":
        bps, spc, reserved, nfats = struct.unpack_from("<HBHB", boot, 11)
        if bps not in (512, 1024, 2048, 4096) or not spc or not reserved or nfats not in (1, 2):
            return None
        total = struct.unpack_from("<I", boot, 32)[0] or struct.unpack_from("<H", boot, 19)[0]
        if not total:
            return None
        fat = source.read_at(off + reserved * bps, 4)
        return FoundPartition(off, total * bps, "fat32", "high" if fat[:1] in (b"\xf8", b"\xf0") else "low")
    if fs == "exfat":
        vol_len = struct.unpack_from("<Q", boot, 72)[0]
        shift = boot[108]
        if not vol_len or not 9 <= shift <= 12:
            return None
        return FoundPartition(off, vol_len << shift, "exfat", "high")
    return None


def scan_lost_partitions(source: BlockSource, *, max_bytes: int | None = None, chunk: int = 4 * 1024 * 1024,
                         progress=None, cancel=None, start: int = 0) -> list[FoundPartition]:
    """Scan for NTFS / FAT32 / exFAT / ext boot structures anywhere on the source (sector-aligned)."""
    limit = min(source.size, max_bytes or source.size)
    patterns = {b"NTFS    ": ("ntfs", 3), b"EXFAT   ": ("exfat", 3), b"FAT32   ": ("fat32", 82)}
    hits: dict[int, FoundPartition] = {}
    pos = start - start % SECTOR
    overlap = 2048
    while pos < limit:
        if cancel and cancel():
            break
        data = source.read_at(pos, chunk + overlap)
        if not data:
            break
        for pat, (fs, rel) in patterns.items():
            i = data.find(pat)
            while i != -1:
                off = pos + i - rel
                if off >= pos and off % SECTOR == 0 and off - pos < chunk and off not in hits:
                    f = _valid_boot(source, off, fs)
                    if f:
                        hits.setdefault(f.start, f)
                i = data.find(pat, i + 1)
        # ext superblock: magic 0xEF53 at +56 of a superblock that sits 1024 bytes into the filesystem
        i = data.find(b"\x53\xef")
        while i != -1:
            sb = pos + i - 56
            off = sb - 1024
            if off >= pos and off % SECTOR == 0 and off - pos < chunk and off not in hits and off >= 0:
                raw = source.read_at(sb, 1024)
                if len(raw) == 1024:
                    blocks = struct.unpack_from("<I", raw, 4)[0]
                    log_bs = struct.unpack_from("<I", raw, 24)[0]
                    state = struct.unpack_from("<H", raw, 58)[0]
                    if blocks and log_bs <= 6 and state in (1, 2, 3, 4, 5, 6, 7):
                        hits[off] = FoundPartition(off, blocks << (10 + log_bs), "ext", "medium")
            i = data.find(b"\x53\xef", i + 1)
        pos += chunk
        if progress:
            progress(min(pos, limit), limit)
    # FAT32 keeps a backup boot sector 6 sectors in; it is not a separate volume
    for start in list(hits):
        f = hits[start]
        if f.fs == "fat32" and (start - 6 * SECTOR) in hits and hits[start - 6 * SECTOR].fs == "fat32":
            del hits[start]
    # a "low confidence" NTFS hit that sits inside a better volume is a stray copy of a boot sector
    result = sorted(hits.values(), key=lambda f: f.start)
    keep: list[FoundPartition] = []
    for f in result:
        inside = any(k is not f and k.start <= f.start < k.start + k.size and k.confidence != "low" for k in result)
        if not (inside and f.confidence == "low"):
            keep.append(f)
    return keep


def found_to_partitions(found: list[FoundPartition]) -> list[Partition]:
    return [Partition(i + 1, f.start, f.size, "found", f.fs, f.fs, note=f.note or f.confidence + " confidence")
            for i, f in enumerate(found)]


def read_partitions(source: BlockSource) -> tuple[str, list[Partition]]:
    """Return ('GPT'|'MBR'|'none', partitions), filling in the filesystem name for each."""
    mbr = source.read_at(0, SECTOR)
    parts = parse_mbr(mbr)
    scheme = "MBR" if parts else "none"
    if any(p.type_desc == "gpt-protective" for p in parts) or source.read_at(SECTOR, 8) == b"EFI PART":
        info = parse_gpt(source)
        parts, scheme = info.entries, "GPT"
    for p in parts:
        p.fs = identify_fs(source, p.start) or p.fs
    return scheme, parts
