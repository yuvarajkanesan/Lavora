"""Writers for small virtual-disk containers (dynamic VHD, VHDX, VMDK sparse + flat, VDI) used to test lavora.core.vdisk."""
import struct
import uuid

MiB = 1 << 20


def _pad(b: bytes, n: int) -> bytes:
    return b + bytes(-len(b) % n)


def make_vhd_dynamic(disk: bytes, block_size: int = 2 * MiB, hole_blocks: tuple[int, ...] = ()) -> bytes:
    n_blocks = (len(disk) + block_size - 1) // block_size
    disk = disk.ljust(n_blocks * block_size, b"\0")
    footer = bytearray(512)
    footer[:8] = b"conectix"
    struct.pack_into(">IIQ", footer, 8, 2, 0x00010000, 512)
    struct.pack_into(">QQ", footer, 40, len(disk), len(disk))
    struct.pack_into(">I", footer, 60, 3)
    hdr = bytearray(1024)
    hdr[:8] = b"cxsparse"
    struct.pack_into(">QQ", hdr, 8, 0xFFFFFFFFFFFFFFFF, 1536)
    struct.pack_into(">II", hdr, 28, n_blocks, block_size)
    bat_len = _pad(bytes(n_blocks * 4), 512)
    sectors = block_size // 512
    bitmap = bytes(((sectors + 7) // 8 + 511) // 512 * 512)
    body, bat, pos = bytearray(), [0xFFFFFFFF] * n_blocks, 1536 + len(bat_len)
    for i in range(n_blocks):
        blk = disk[i * block_size:(i + 1) * block_size]
        if i in hole_blocks or not any(blk):
            continue
        bat[i] = pos // 512
        body += bitmap + blk
        pos += len(bitmap) + len(blk)
    batb = _pad(b"".join(struct.pack(">I", v) for v in bat), 512)
    return bytes(footer) + bytes(hdr) + batb + bytes(body) + bytes(footer)


def make_vdi(disk: bytes, block_size: int = MiB) -> bytes:
    n_blocks = (len(disk) + block_size - 1) // block_size
    disk = disk.ljust(n_blocks * block_size, b"\0")
    off_blocks, off_data = 512, 512 + _pad(bytes(n_blocks * 4), 512).__len__()
    h = bytearray(512)
    h[:36] = b"<<< Oracle VM VirtualBox Disk Image >>>\n"[:36]
    struct.pack_into("<IIII", h, 64, 0xBEDA107F, 0x00010001, 400, 1)
    struct.pack_into("<II", h, 340, off_blocks, off_data)
    struct.pack_into("<Q", h, 368, len(disk))
    struct.pack_into("<III", h, 376, block_size, 0, n_blocks)
    mapping, body = [], bytearray()
    for i in range(n_blocks):
        blk = disk[i * block_size:(i + 1) * block_size]
        if not any(blk):
            mapping.append(0xFFFFFFFF)
            continue
        mapping.append(len(body) // block_size)
        body += blk
    table = _pad(b"".join(struct.pack("<I", v) for v in mapping), 512)
    return bytes(h) + table + bytes(body)


def make_vmdk_sparse(disk: bytes, grain_sectors: int = 128) -> bytes:
    grain = grain_sectors * 512
    n_grains = (len(disk) + grain - 1) // grain
    disk = disk.ljust(n_grains * grain, b"\0")
    gtes = 512
    n_gt = (n_grains + gtes - 1) // gtes
    gd_sector, gt_sector = 21, 22
    first_grain = gt_sector + n_gt * 4 + 8
    gt, body = [[0] * gtes for _ in range(n_gt)], bytearray()
    for g in range(n_grains):
        blk = disk[g * grain:(g + 1) * grain]
        if not any(blk):
            continue
        gt[g // gtes][g % gtes] = first_grain + len(body) // 512
        body += blk
    h = bytearray(512)
    h[:4] = b"KDMV"
    struct.pack_into("<II", h, 4, 1, 3)
    struct.pack_into("<QQ", h, 12, len(disk) // 512, grain_sectors)
    struct.pack_into("<QQ", h, 28, 1, 20)
    struct.pack_into("<I", h, 44, gtes)
    struct.pack_into("<QQQ", h, 48, 0, gd_sector, first_grain)
    out = bytearray(first_grain * 512)
    out[:512] = h
    gd = b"".join(struct.pack("<I", gt_sector + i * 4) for i in range(n_gt))
    out[gd_sector * 512:gd_sector * 512 + len(gd)] = gd
    for i, table in enumerate(gt):
        raw = b"".join(struct.pack("<I", v) for v in table)
        out[(gt_sector + i * 4) * 512:(gt_sector + i * 4) * 512 + len(raw)] = raw
    return bytes(out) + bytes(body)


def make_vhdx(disk: bytes, block_size: int = MiB) -> bytes:
    n_blocks = (len(disk) + block_size - 1) // block_size
    disk = disk.ljust(n_blocks * block_size, b"\0")
    g = lambda s: uuid.UUID(s).bytes_le  # noqa: E731
    ratio = (1 << 23) * 512 // block_size
    n_bat = n_blocks + n_blocks // ratio + 1
    bat_off, meta_off, data_off = MiB, 2 * MiB, 4 * MiB
    bat, body = [0] * n_bat, bytearray()
    for i in range(n_blocks):
        blk = disk[i * block_size:(i + 1) * block_size]
        if not any(blk):
            bat[i + i // ratio] = 2  # zero block
            continue
        off = data_off + len(body)
        bat[i + i // ratio] = (off >> 20) << 20 | 6
        body += blk
    out = bytearray(data_off)
    out[:8] = b"vhdxfile"
    out[64 * 1024:64 * 1024 + 4] = b"head"
    out[128 * 1024:128 * 1024 + 4] = b"head"
    rt = bytearray(64 * 1024)
    rt[:4] = b"regi"
    struct.pack_into("<I", rt, 8, 2)
    for i, (guid, off, ln) in enumerate(((g("2DC27766-F623-4200-9D64-115E9BFD4A08"), bat_off, n_bat * 8),
                                         (g("8B7CA206-4790-4B9A-B8FE-575F3FB84C98"), meta_off, 64 * 1024))):
        rt[16 + 32 * i:16 + 32 * i + 16] = guid
        struct.pack_into("<QII", rt, 32 + 32 * i, off, ln, 1)
    out[192 * 1024:192 * 1024 + len(rt)] = rt
    out[bat_off:bat_off + n_bat * 8] = b"".join(struct.pack("<Q", v) for v in bat)
    meta = bytearray(64 * 1024)
    meta[:8] = b"metadata"
    struct.pack_into("<H", meta, 10, 3)
    items = [(g("CAA16737-FA36-4D43-B3B6-33F0AA44E76B"), struct.pack("<II", block_size, 0)),
             (g("2FA54224-CD1B-4876-B211-5DBED83BF4B8"), struct.pack("<Q", len(disk))),
             (g("8141BF1D-A96F-4709-BA47-F233A8FAAB5F"), struct.pack("<I", 512))]
    pos = 4096
    for i, (guid, val) in enumerate(items):
        meta[32 + 32 * i:48 + 32 * i] = guid
        struct.pack_into("<III", meta, 48 + 32 * i, pos, len(val), 4)
        meta[pos:pos + len(val)] = val
        pos += 64
    out[meta_off:meta_off + len(meta)] = meta
    return bytes(out) + bytes(body)
