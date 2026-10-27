import hashlib
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture(autouse=True)
def lavora_home(tmp_path, monkeypatch):
    """Every test gets its own data dir (undo store, audit log, licence) - nothing touches the real profile."""
    home = tmp_path / "lavora-home"
    monkeypatch.setenv("LAVORA_HOME", str(home))
    return home


def payload(seed: int, size: int) -> bytes:
    rnd = random.Random(seed)
    return bytes(rnd.getrandbits(8) for _ in range(size)) if size < 4096 else rnd.randbytes(size)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def write_img(tmp_path):
    def _write(data: bytes, name: str = "disk.img") -> str:
        p = tmp_path / name
        p.write_bytes(data)
        return str(p)
    return _write


@pytest.fixture(autouse=True)
def _close_gui_windows():
    """Close every window a test created, so worker threads are stopped and joined before Qt objects are torn down."""
    yield
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        return
    app = QApplication.instance()
    if app is not None:
        for w in app.topLevelWidgets():
            w.close()
        app.processEvents()
