"""Virtual RAID: layouts, degraded reads (XOR / GF(256) P+Q), automatic detection from shuffled members."""
import itertools

import pytest
from conftest import payload, sha
from imgbuilders import Fat32Builder, NtfsBuilder, make_mbr_disk

from lavora.cli.main import main
from lavora.core import raid
from lavora.core.device import BytesSource
from lavora.core.fs import open_filesystem


# ---- independent reference writers (formulas written out here, not imported from lavora)
def _gmul(a, b):
    r = 0
    while b:
        if b & 1:
            r ^= a
        a <<= 1
        if a & 0x100:
            a ^= 0x11D
        b >>= 1
    return r


def _gpow2(k):
    x = 1
    for _ in range(k):
        x = _gmul(x, 2)
    return x


def _xor_all(blocks):
    out = bytearray(len(blocks[0]))
    for b in blocks:
        for i, v in enumerate(b):
            out[i] ^= v
    return bytes(out)


def _q(blocks):
    out = bytearray(len(blocks[0]))
    for k, b in enumerate(blocks):
        g = _gpow2(k)
        for i, v in enumerate(b):
            out[i] ^= _gmul(v, g)
    return bytes(out)


def pad(data, unit):
    return data + bytes(-len(data) % unit)


def make_raid(data, level, n, chunk, layout="ls"):
    members = [bytearray() for _ in range(n)]
    if level == 0:
        data = pad(data, n * chunk)
        for r in range(len(data) // (n * chunk)):
            for d in range(n):
                members[d] += data[(r * n + d) * chunk:(r * n + d + 1) * chunk]
    elif level == 1:
        for m in members:
            m += data
    elif level == 10:
        pairs = n // 2
        data = pad(data, pairs * chunk)
        for r in range(len(data) // (pairs * chunk)):
            for p in range(pairs):
                c = data[(r * pairs + p) * chunk:(r * pairs + p + 1) * chunk]
                members[2 * p] += c
                members[2 * p + 1] += c
    elif level == 5:
        k = n - 1
        data = pad(data, k * chunk)
        for r in range(len(data) // (k * chunk)):
            pd = (n - 1 - r % n) if layout in ("ls", "la") else r % n
            row = [None] * n
            chunks = [data[(r * k + d) * chunk:(r * k + d + 1) * chunk] for d in range(k)]
            for d, c in enumerate(chunks):
                disk = (pd + 1 + d) % n if layout in ("ls", "rs") else (d if d < pd else d + 1)
                row[disk] = c
            row[pd] = _xor_all(chunks)
            for i in range(n):
                members[i] += row[i]
    elif level == 6:
        k = n - 2
        data = pad(data, k * chunk)
        for r in range(len(data) // (k * chunk)):
            pd = n - 1 - r % n
            qd = (pd + 1) % n
            chunks = [data[(r * k + d) * chunk:(r * k + d + 1) * chunk] for d in range(k)]
            row = [None] * n
            for d, c in enumerate(chunks):
                row[(pd + 2 + d) % n] = c
            row[pd], row[qd] = _xor_all(chunks), _q(chunks)
            for i in range(n):
                members[i] += row[i]
    return [bytes(m) for m in members]


def srcs(images, missing=()):
    return [None if i in missing else BytesSource(m, f"m{i}") for i, m in enumerate(images)]


DATA = payload(7, 300_000)
CHUNK = 4096


@pytest.mark.parametrize("level,n,layout", [(0, 3, "ls"), (1, 3, "ls"), (10, 4, "ls"), (5, 3, "ls"), (5, 4, "la"), (5, 4, "rs"),
                                            (5, 5, "ra"), (6, 4, "ls"), (6, 6, "ls")])
def test_array_reads_back_bit_exact(level, n, layout):
    arr = raid.RaidSource(srcs(make_raid(DATA, level, n, CHUNK, layout)), level, CHUNK, layout)
    assert arr.read_at(0, len(DATA)) == DATA
    assert arr.read_at(CHUNK - 10, 20) == DATA[CHUNK - 10:CHUNK + 10]  # straddling a chunk boundary
    assert arr.size >= len(DATA)


@pytest.mark.parametrize("layout", ["ls", "la", "rs", "ra"])
def test_raid5_survives_any_single_missing_member(layout):
    imgs = make_raid(DATA, 5, 4, CHUNK, layout)
    for gone in range(4):
        arr = raid.RaidSource(srcs(imgs, {gone}), 5, CHUNK, layout)
        assert arr.read_at(0, len(DATA)) == DATA, f"member {gone} missing"


def test_raid6_survives_any_one_or_two_missing_members():
    imgs = make_raid(DATA, 6, 6, CHUNK)
    for gone in itertools.chain(((i,) for i in range(6)), itertools.combinations(range(6), 2)):
        arr = raid.RaidSource(srcs(imgs, set(gone)), 6, CHUNK)
        assert arr.read_at(0, len(DATA)) == DATA, f"members {gone} missing"


def test_raid10_and_raid1_degraded():
    imgs = make_raid(DATA, 10, 4, CHUNK)
    for gone in ({0, 2}, {1, 3}, {0, 3}):
        assert raid.RaidSource(srcs(imgs, gone), 10, CHUNK).read_at(0, len(DATA)) == DATA
    with pytest.raises(raid.RaidError):
        raid.RaidSource(srcs(imgs, {0, 1}), 10, CHUNK)
    one = make_raid(DATA, 1, 3, CHUNK)
    assert raid.RaidSource(srcs(one, {0, 1}), 1, CHUNK).read_at(0, 100) == DATA[:100]


def test_unrecoverable_configurations_are_refused():
    imgs = make_raid(DATA, 5, 4, CHUNK)
    with pytest.raises(raid.RaidError, match="cannot be reconstructed"):
        raid.RaidSource(srcs(imgs, {0, 1}), 5, CHUNK)
    with pytest.raises(raid.RaidError):
        raid.RaidSource(srcs(make_raid(DATA, 0, 3, CHUNK), {1}), 0, CHUNK)
    with pytest.raises(raid.RaidError):
        raid.RaidSource(srcs(make_raid(DATA, 5, 3, CHUNK)[:2]), 5, CHUNK)  # RAID 5 needs 3+ members


def test_gf256_arithmetic():
    for a in (1, 2, 3, 0x53, 0xCA, 255):
        for b in (1, 2, 7, 0x8E, 254):
            assert raid.gf_mul(a, b) == _gmul(a, b)
            assert raid.gf_div(raid.gf_mul(a, b), b) == a


# ------------------------------------------------------------------ detection
def _jpeg(n, seed):
    return b"\xff\xd8\xff\xe0" + payload(seed, n).replace(b"\xff", b"\xfe") + b"\xff\xd9"


def volume_disk():
    ntfs = NtfsBuilder(total_sectors=24576)
    ntfs.add_file("Docs/notes.txt", b"line of plain text\n" * 4000)
    ntfs.add_file("Photos/a.jpg", _jpeg(150_000, 1))
    ntfs.add_file("Photos/b.jpg", _jpeg(90_000, 2))
    ntfs.add_file("Docs/c.pdf", b"%PDF-1.4\n" + payload(3, 100_000).replace(b"%", b"x") + b"\nstartxref\n0\n%%EOF\n")
    ntfs.add_file("Docs/file2.bin", payload(42, 5200))
    ntfs.delete("Docs/file2.bin")
    fat = Fat32Builder()
    fat.add_file("A.TXT", b"hello")
    disk, _ = make_mbr_disk([(ntfs.build(), 0x07), (fat.build(), 0x0C)])
    return disk


@pytest.mark.parametrize("level,n,chunk,layout", [(5, 4, 65536, "ls"), (5, 3, 32768, "la"), (5, 4, 16384, "rs"), (5, 3, 65536, "ra"),
                                                  (0, 3, 65536, "ls")])
def test_detection_recovers_a_shuffled_array(level, n, chunk, layout):
    disk = volume_disk()
    imgs = make_raid(disk, level, n, chunk, layout)
    order = list(range(n))[::-1]  # members handed over in the wrong order
    shuffled = [BytesSource(imgs[i], f"disk{i}.img") for i in order]
    cands = raid.detect(shuffled, chunk_sizes=(16384, 32768, 65536, 131072), levels=(level,))
    assert cands, "no candidate"
    best = cands[0]
    arr = raid.RaidSource([shuffled[i] for i in best.order], best.level, best.chunk, best.layout)
    assert arr.read_at(0, len(disk)) == disk  # the detected parameters rebuild the exact original disk
    # and a filesystem on it is usable end to end
    _s, parts = __import__("lavora.core.partitions", fromlist=["x"]).read_partitions(arr)
    fs = open_filesystem(__import__("lavora.core.device", fromlist=["x"]).PartitionView(arr, parts[0].start, parts[0].size))
    deleted = [e for e in fs.scan() if e.deleted]
    assert deleted and sha(fs.read(deleted[0], 0, deleted[0].size)) == sha(payload(42, 5200))


def test_detection_levels_are_guessed_from_the_data():
    disk = volume_disk()
    r5 = [BytesSource(m) for m in make_raid(disk, 5, 4, 65536, "ls")]
    r0 = [BytesSource(m) for m in make_raid(disk, 0, 3, 65536)]
    r1 = [BytesSource(m) for m in make_raid(disk, 1, 2, 65536)]
    assert raid.looks_like_raid5(r5) > 0.95 and raid.looks_like_raid5(r0) < 0.5
    assert raid.detect(r1)[0].level == 1
    assert raid.detect(r5, chunk_sizes=(65536,))[0].level == 5


def test_detection_returns_nothing_for_unrelated_disks():
    members = [BytesSource(payload(i, 1 << 20)) for i in range(3)]
    assert raid.detect(members, chunk_sizes=(65536,)) == []


# ------------------------------------------------------------------ spec strings and CLI
def test_spec_roundtrip_and_cli_use(tmp_path, capsys):
    disk = volume_disk()
    paths = []
    for i, m in enumerate(make_raid(disk, 5, 4, 65536, "ls")):
        p = tmp_path / f"m{i}.img"
        p.write_bytes(m)
        paths.append(str(p))
    spec = raid.spec_for(5, 65536, "ls", paths)
    with raid.open_raid_spec(spec) as arr:
        assert arr.read_at(0, len(disk)) == disk
    degraded = raid.spec_for(5, 65536, "ls", [paths[0], None, paths[2], paths[3]])  # one disk lost
    with raid.open_raid_spec(degraded) as arr:
        assert arr.read_at(0, len(disk)) == disk
    assert main(["scan", spec, "--partition", "1", "--deleted-only"]) == 0
    assert "Docs/file2.bin" in capsys.readouterr().out
    assert main(["raid-detect", *reversed(paths), "--chunks", "64"]) == 0
    out = capsys.readouterr().out
    assert "RAID 5" in out and "raid:level=5" in out
