"""`Lavora.exe --selftest`: exercises the frozen build end to end (GUI construction, crypto, carving, partition code).

The build fails if this fails, so a missing DLL or a trimmed-away module is caught before an installer is made.
"""
from __future__ import annotations

import os
import struct
import sys
import tempfile
import traceback


def run() -> int:
    log = os.path.join(tempfile.gettempdir(), "lavora-selftest.log")
    lines: list[str] = []

    def check(name: str, fn) -> bool:
        try:
            fn()
            lines.append(f"ok    {name}")
            return True
        except Exception:  # noqa: BLE001
            lines.append(f"FAIL  {name}\n{traceback.format_exc()}")
            return False

    def gui() -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        from .gui.app import build_window
        app = QApplication.instance() or QApplication([])
        win = build_window(app)  # type: ignore[arg-type]
        assert len(win.pages) >= 11 and "auto" in win.pages and "health" in win.pages

    def crypto_and_backup() -> None:
        import shutil

        from .core import backup
        d = tempfile.mkdtemp(prefix="lavora-st-")
        try:
            src = os.path.join(d, "s")
            os.makedirs(src)
            with open(os.path.join(src, "a.txt"), "wb") as f:
                f.write(b"selftest" * 5000)
            repo = backup.Repository.init(os.path.join(d, "r"), "pw")
            st = backup.backup(repo, [src])
            assert backup.verify_backup(repo, st.snapshot_id).ok
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def disk_logic() -> None:
        from .core import carver, partitions
        from .core.device import BytesSource
        disk = bytearray(2 * 1024 * 1024)
        mbr = partitions.build_mbr([partitions.Partition(1, 1 << 20, 1 << 20, "x", fs="ntfs")])
        disk[:512] = mbr
        jpg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x01" * 200 + b"\xff\xd9"
        disk[4096:4096 + len(jpg)] = jpg
        src = BytesSource(bytes(disk))
        assert [p.start for p in partitions.parse_mbr(bytes(disk[:512]))] == [1 << 20]
        found = list(carver.carve(src))
        assert len(found) == 1 and found[0].offset == 4096 and found[0].size == len(jpg)
        assert struct.calcsize("<I") == 4

    def licence() -> None:
        import base64

        from .core import license
        assert len(base64.b64decode(license.PUBLIC_KEY_B64)) == 32
        assert not license.verify_key("LAVORA-AAAA.AAAA").valid

    def cli() -> None:
        from .cli.main import build_parser
        assert build_parser().parse_args(["version"]).cmd == "version"

    ok = all([check("gui", gui), check("backup+crypto", crypto_and_backup), check("carve+partitions", disk_logic),
              check("licence", licence), check("cli", cli)])
    with open(log, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
