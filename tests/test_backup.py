"""Backup: incremental, compressed, AES-256-GCM, verification by test-restore, safety checks."""
import os

import pytest
from conftest import payload, sha

from lavora.core import backup
from lavora.core.safety import SafetyError


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "data"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_bytes(b"alpha " * 1000)
    (root / "sub" / "b.bin").write_bytes(payload(1, 9 * 1024 * 1024))  # spans 3 chunks
    (root / "sub" / "empty.txt").write_bytes(b"")
    return root


def test_backup_restore_roundtrip(tmp_path, tree):
    repo = backup.Repository.init(str(tmp_path / "repo"))
    st = backup.backup(repo, [str(tree)])
    assert st.files == 3 and not st.errors
    out = tmp_path / "restored"
    backup.restore(repo, st.snapshot_id, str(out))
    for rel in ("a.txt", "sub/b.bin", "sub/empty.txt"):
        assert (out / "data" / rel).read_bytes() == (tree / rel).read_bytes()


def test_second_run_is_incremental(tmp_path, tree):
    repo = backup.Repository.init(str(tmp_path / "repo"))
    backup.backup(repo, [str(tree)])
    import time
    time.sleep(1.1)  # snapshot ids are per-second
    s2 = backup.backup(repo, [str(tree)])
    assert s2.unchanged == 3 and s2.bytes_stored == 0
    (tree / "a.txt").write_bytes(b"changed " * 500)
    time.sleep(1.1)
    s3 = backup.backup(repo, [str(tree)])
    assert s3.unchanged == 2 and 0 < s3.bytes_stored < 10_000
    assert len(repo.snapshots()) == 3
    out = tmp_path / "old"
    backup.restore(repo, repo.snapshots()[0], str(out))  # the first snapshot still has the original content
    assert (out / "data" / "a.txt").read_bytes() == b"alpha " * 1000


def test_encrypted_repo_hides_content_and_needs_the_password(tmp_path, tree):
    repo = backup.Repository.init(str(tmp_path / "repo"), "correct horse")
    st = backup.backup(repo, [str(tree)])
    blobs = b"".join(open(os.path.join(r, f), "rb").read() for r, _d, fs in os.walk(tmp_path / "repo" / "blobs") for f in fs)
    assert b"alpha alpha" not in blobs
    manifest = (tmp_path / "repo" / "snapshots" / f"{st.snapshot_id}.json").read_bytes()
    assert b"a.txt" not in manifest  # file names are sealed too
    with pytest.raises(backup.WrongPassword):
        backup.Repository.open(str(tmp_path / "repo"), "wrong")
    with pytest.raises(backup.WrongPassword):
        backup.Repository.open(str(tmp_path / "repo"), None)
    ok = backup.Repository.open(str(tmp_path / "repo"), "correct horse")
    assert backup.verify_backup(ok).ok


def test_verify_detects_corruption_and_missing_blobs(tmp_path, tree):
    repo = backup.Repository.init(str(tmp_path / "repo"), "pw")
    backup.backup(repo, [str(tree)])
    blob_files = [os.path.join(r, f) for r, _d, fs in os.walk(tmp_path / "repo" / "blobs") for f in fs]
    with open(blob_files[0], "r+b") as f:  # flip a byte: GCM authentication must catch it
        f.seek(20)
        b = f.read(1)
        f.seek(20)
        f.write(bytes([b[0] ^ 1]))
    rep = backup.verify_backup(repo)
    assert not rep.ok and any("authentication" in x or "corrupt" in x for x in rep.failures)


def test_verify_detects_missing_blobs(tmp_path, tree):
    repo = backup.Repository.init(str(tmp_path / "repo"))
    backup.backup(repo, [str(tree)])
    victim = next(os.path.join(r, f) for r, _d, fs in os.walk(tmp_path / "repo" / "blobs") for f in fs)
    os.remove(victim)
    rep = backup.verify_backup(repo)
    assert not rep.ok and any("missing" in x for x in rep.failures)


def test_unencrypted_corruption_is_detected(tmp_path, tree):
    repo = backup.Repository.init(str(tmp_path / "repo"))
    backup.backup(repo, [str(tree)])
    victim = next(os.path.join(r, f) for r, _d, fs in os.walk(tmp_path / "repo" / "blobs") for f in fs)
    data = open(victim, "rb").read()
    open(victim, "wb").write(data[:10] + b"\0" * 10 + data[20:])
    assert not backup.verify_backup(repo).ok


def test_restore_never_overwrites_by_default(tmp_path, tree):
    repo = backup.Repository.init(str(tmp_path / "repo"))
    st = backup.backup(repo, [str(tree)])
    out = tmp_path / "out"
    backup.restore(repo, st.snapshot_id, str(out))
    with pytest.raises(SafetyError):
        backup.restore(repo, st.snapshot_id, str(out))
    backup.restore(repo, st.snapshot_id, str(out), overwrite=True)


def test_restore_blocks_path_traversal_in_a_tampered_manifest(tmp_path, tree):
    repo = backup.Repository.init(str(tmp_path / "repo"))
    st = backup.backup(repo, [str(tree)])
    m = repo.read_snapshot(st.snapshot_id)
    m["files"][0]["path"] = "../../evil.txt"
    repo._write_snapshot(st.snapshot_id, m)
    with pytest.raises(SafetyError):
        backup.restore(repo, st.snapshot_id, str(tmp_path / "out"))
    assert not (tmp_path / "evil.txt").exists()


def test_destination_inside_source_is_refused(tmp_path, tree):
    repo = backup.Repository.init(str(tree / "backups"))
    with pytest.raises(SafetyError):
        backup.backup(repo, [str(tree)])


def test_sftp_is_reported_as_not_implemented():
    with pytest.raises(NotImplementedError):
        backup.Repository.init("sftp://host/path")


def test_unreadable_file_is_skipped_and_reported(tmp_path, tree, monkeypatch):
    real_open = open

    def flaky(path, *a, **k):
        if str(path).endswith("a.txt") and "rb" in (a[0] if a else k.get("mode", "")):
            raise PermissionError("locked")
        return real_open(path, *a, **k)
    repo = backup.Repository.init(str(tmp_path / "repo"))
    monkeypatch.setattr("builtins.open", flaky)
    st = backup.backup(repo, [str(tree)])
    monkeypatch.undo()
    assert any("a.txt" in e for e in st.errors) and st.files == 2


def test_hash_of_restored_file_matches_manifest(tmp_path, tree):
    repo = backup.Repository.init(str(tmp_path / "repo"))
    st = backup.backup(repo, [str(tree)])
    m = repo.read_snapshot(st.snapshot_id)
    big = next(f for f in m["files"] if f["path"].endswith("b.bin"))
    assert big["sha256"] == sha((tree / "sub" / "b.bin").read_bytes()) and len(big["chunks"]) == 3
