"""File-level backup: incremental, compressed, optionally AES-256-GCM encrypted, with test-restore verification.

Layout of a backup repository folder (a local path, external disk, or an SMB share's UNC path):
  repo.json          settings (+ KDF salt and a password check token when encrypted)
  blobs/xx/<id>      4 MiB chunks, zlib-compressed, then AES-256-GCM sealed (nonce || ciphertext) when encrypted
  snapshots/<id>.json  one manifest per run listing every file and its chunk ids (sealed when encrypted)
Chunks are content-addressed, so unchanged data is stored once: every run is a full snapshot that costs only the
changed bytes (incremental). SFTP and cloud destinations are not implemented yet.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import shutil
import tempfile
import time
import zlib
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .safety import SafetyError

CHUNK = 4 * 1024 * 1024
TOKEN = b"lavora-backup-ok"


class BackupError(Exception):
    pass


class WrongPassword(BackupError):
    pass


def open_destination(spec: str) -> str:
    """Local folders, external disks and SMB shares (UNC paths) are plain paths."""
    if spec.lower().startswith(("sftp://", "s3://", "gs://", "azure://")):
        raise NotImplementedError("SFTP/cloud destinations are not implemented yet (needs an SSH library: approval pending).")
    return spec


class Repository:
    def __init__(self, path: str, key: bytes | None, salt: bytes | None):
        self.path, self.key, self.salt = path, key, salt

    # ---------------------------------------------------------------- open/create
    @staticmethod
    def _derive(password: str, salt: bytes) -> bytes:
        return Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(password.encode())

    @classmethod
    def init(cls, path: str, password: str | None = None) -> Repository:
        path = open_destination(path)
        os.makedirs(os.path.join(path, "blobs"), exist_ok=True)
        os.makedirs(os.path.join(path, "snapshots"), exist_ok=True)
        meta: dict = {"version": 1, "encrypted": bool(password), "created": time.time()}
        key = salt = None
        if password:
            salt = os.urandom(16)
            key = cls._derive(password, salt)
            meta["salt"] = base64.b64encode(salt).decode()
            nonce = os.urandom(12)
            meta["check"] = base64.b64encode(nonce + AESGCM(key).encrypt(nonce, TOKEN, None)).decode()
        with open(os.path.join(path, "repo.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f)
        return cls(path, key, salt)

    @classmethod
    def open(cls, path: str, password: str | None = None) -> Repository:
        path = open_destination(path)
        try:
            with open(os.path.join(path, "repo.json"), encoding="utf-8") as f:
                meta = json.load(f)
        except OSError as ex:
            raise BackupError(f"{path} is not a Lavora backup repository") from ex
        if not meta.get("encrypted"):
            return cls(path, None, None)
        if not password:
            raise WrongPassword("This backup is encrypted; a password is required.")
        salt = base64.b64decode(meta["salt"])
        key = cls._derive(password, salt)
        raw = base64.b64decode(meta["check"])
        try:
            if AESGCM(key).decrypt(raw[:12], raw[12:], None) != TOKEN:
                raise WrongPassword("Wrong password.")
        except InvalidTag as ex:
            raise WrongPassword("Wrong password.") from ex
        return cls(path, key, salt)

    # ---------------------------------------------------------------- sealing
    def _seal(self, data: bytes) -> bytes:
        if self.key is None:
            return data
        nonce = os.urandom(12)
        return nonce + AESGCM(self.key).encrypt(nonce, data, None)

    def _unseal(self, data: bytes) -> bytes:
        if self.key is None:
            return data
        try:
            return AESGCM(self.key).decrypt(data[:12], data[12:], None)
        except InvalidTag as ex:
            raise BackupError("blob failed authentication (corrupt or tampered)") from ex

    def _blob_id(self, plain: bytes) -> str:
        # keyed when encrypted, so equal data is not recognisable by someone who only has the repository
        return (hmac.new(self.key, plain, hashlib.sha256) if self.key else hashlib.sha256(plain)).hexdigest()

    def _blob_path(self, blob_id: str) -> str:
        return os.path.join(self.path, "blobs", blob_id[:2], blob_id)

    def put_chunk(self, plain: bytes) -> tuple[str, bool]:
        bid = self._blob_id(plain)
        p = self._blob_path(bid)
        if os.path.exists(p):
            return bid, False
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "wb") as f:
            f.write(self._seal(zlib.compress(plain, 6)))
        os.replace(tmp, p)
        return bid, True

    def get_chunk(self, bid: str) -> bytes:
        try:
            with open(self._blob_path(bid), "rb") as f:
                raw = f.read()
        except OSError as ex:
            raise BackupError(f"missing blob {bid[:12]}") from ex
        try:
            return zlib.decompress(self._unseal(raw))
        except zlib.error as ex:
            raise BackupError(f"blob {bid[:12]} is corrupt") from ex

    # ---------------------------------------------------------------- snapshots
    def _write_snapshot(self, sid: str, manifest: dict) -> None:
        raw = json.dumps(manifest).encode()
        with open(os.path.join(self.path, "snapshots", sid + ".json"), "wb") as f:
            f.write(self._seal(raw))

    def read_snapshot(self, sid: str) -> dict:
        with open(os.path.join(self.path, "snapshots", sid + ".json"), "rb") as f:
            return json.loads(self._unseal(f.read()))

    def snapshots(self) -> list[str]:
        d = os.path.join(self.path, "snapshots")
        return sorted(n[:-5] for n in os.listdir(d) if n.endswith(".json"))


@dataclass
class BackupStats:
    snapshot_id: str = ""
    files: int = 0
    unchanged: int = 0
    bytes_total: int = 0
    bytes_stored: int = 0  # new blob bytes (before compression)
    errors: list[str] = field(default_factory=list)


def backup(repo: Repository, sources: list[str], *, progress=None) -> BackupStats:
    stats = BackupStats(snapshot_id=time.strftime("%Y%m%d-%H%M%S"))
    prev: dict[str, dict] = {}
    if repo.snapshots():
        prev = {f["path"]: f for f in repo.read_snapshot(repo.snapshots()[-1])["files"]}
    repo_abs = os.path.abspath(repo.path)
    files: list[dict] = []
    for src in sources:
        src = os.path.abspath(src)
        if repo_abs == src or repo_abs.startswith(src + os.sep):
            raise SafetyError("The backup destination is inside the folder being backed up.")
        entries = []
        if os.path.isfile(src):
            entries = [(src, os.path.basename(src))]
        else:
            base = os.path.dirname(src)
            for root, dirs, names in os.walk(src):
                dirs[:] = [d for d in dirs if os.path.abspath(os.path.join(root, d)) != repo_abs]
                entries += [(os.path.join(root, n), os.path.relpath(os.path.join(root, n), base)) for n in names]
        for full, rel in entries:
            rel = rel.replace("\\", "/")
            try:
                st = os.stat(full)
                old = prev.get(rel)
                if old and old["size"] == st.st_size and old["mtime_ns"] == st.st_mtime_ns and \
                        all(os.path.exists(repo._blob_path(c)) for c in old["chunks"]):
                    files.append(old)
                    stats.unchanged += 1
                    stats.files += 1
                    stats.bytes_total += st.st_size
                    continue
                h, chunks = hashlib.sha256(), []
                with open(full, "rb") as f:
                    while block := f.read(CHUNK):
                        h.update(block)
                        bid, new = repo.put_chunk(block)
                        chunks.append(bid)
                        stats.bytes_stored += len(block) if new else 0
                files.append({"path": rel, "size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": h.hexdigest(),
                              "chunks": chunks})
                stats.files += 1
                stats.bytes_total += st.st_size
            except OSError as ex:
                stats.errors.append(f"{full}: {ex}")
            if progress:
                progress(stats.files, 0)
    repo._write_snapshot(stats.snapshot_id, {"created": time.time(), "sources": sources, "files": files})
    return stats


def restore(repo: Repository, snapshot_id: str, dest: str, *, overwrite: bool = False, only: str = "") -> list[str]:
    """Restore a snapshot into `dest`. Never overwrites unless asked; refuses path traversal in manifests."""
    manifest = repo.read_snapshot(snapshot_id)
    out = []
    base = os.path.abspath(dest)
    for f in manifest["files"]:
        if only and only.lower() not in f["path"].lower():
            continue
        target = os.path.abspath(os.path.join(base, *f["path"].split("/")))
        if not target.startswith(base + os.sep):
            raise SafetyError(f"manifest path escapes the restore folder: {f['path']}")
        if os.path.exists(target) and not overwrite:
            raise SafetyError(f"{target} exists; pass overwrite to replace it.")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as w:
            for cid in f["chunks"]:
                w.write(repo.get_chunk(cid))
        os.utime(target, ns=(f["mtime_ns"], f["mtime_ns"]))
        out.append(target)
    return out


@dataclass
class VerifyReport:
    snapshot_id: str
    files_checked: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


def verify_backup(repo: Repository, snapshot_id: str | None = None) -> VerifyReport:
    """Test-restore a snapshot into a throw-away sandbox folder and compare SHA-256 of every file."""
    sid = snapshot_id or (repo.snapshots() or [""])[-1]
    if not sid:
        raise BackupError("no snapshots to verify")
    rep = VerifyReport(sid)
    sandbox = tempfile.mkdtemp(prefix="lavora-verify-")
    try:
        manifest = repo.read_snapshot(sid)
        for f in manifest["files"]:
            rep.files_checked += 1
            try:
                h = hashlib.sha256()
                path = os.path.join(sandbox, f"{rep.files_checked}.bin")
                with open(path, "wb") as w:
                    for cid in f["chunks"]:
                        block = repo.get_chunk(cid)
                        h.update(block)
                        w.write(block)
                if h.hexdigest() != f["sha256"] or os.path.getsize(path) != f["size"]:
                    rep.failures.append(f"{f['path']}: content does not match the manifest")
            except BackupError as ex:
                rep.failures.append(f"{f['path']}: {ex}")
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
    return rep
