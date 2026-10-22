"""Save a scan and resume it later without rescanning (JSON lines, written as the scan streams, so a cancelled or crashed
scan of a 5 TB disk still leaves everything found so far).

  line 1   {"lavora_session": 1, "source": ..., "source_size": ..., "volume_offset": ..., "volume_size": ..., "fs": ...}
  lines    one FileEntry each
  last     {"end": true, "count": N}      (missing when the scan did not finish: the session is then marked incomplete)
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from .device import BlockSource, PartitionView, open_source
from .fs import Filesystem, open_filesystem
from .fs.base import FileEntry, FsError


@dataclass
class SessionHeader:
    source: str
    source_size: int
    volume_offset: int
    volume_size: int
    fs: str = ""
    complete: bool = False
    count: int = 0


class SessionWriter:
    def __init__(self, path: str, source: str, source_size: int, volume_offset: int, volume_size: int, fs: str):
        self.path, self.count = path, 0
        self._f = open(path, "w", encoding="utf-8")  # noqa: SIM115 - closed in close()
        self._f.write(json.dumps({"lavora_session": 1, "source": source, "source_size": source_size,
                                  "volume_offset": volume_offset, "volume_size": volume_size, "fs": fs}) + "\n")

    def add(self, e: FileEntry) -> None:
        self._f.write(json.dumps(e.to_dict()) + "\n")
        self.count += 1
        if self.count % 5000 == 0:
            self._f.flush()

    def close(self, complete: bool) -> None:
        if complete:
            self._f.write(json.dumps({"end": True, "count": self.count}) + "\n")
        self._f.close()

    def tee(self, entries: Iterable[FileEntry], cancel=None) -> Iterator[FileEntry]:
        """Pass entries through while saving them; marks the session complete only if the iterator ran to the end."""
        finished = False
        try:
            for e in entries:
                self.add(e)
                yield e
            finished = not (cancel and cancel())
        finally:
            self.close(finished)


def read_header(path: str) -> SessionHeader:
    with open(path, encoding="utf-8") as f:
        h = json.loads(f.readline())
    if h.get("lavora_session") != 1:
        raise FsError("not a Lavora session file")
    return SessionHeader(h["source"], h["source_size"], h["volume_offset"], h["volume_size"], h.get("fs", ""))


def read_entries(path: str) -> Iterator[FileEntry]:
    with open(path, encoding="utf-8") as f:
        f.readline()
        for line in f:
            d = json.loads(line)
            if d.get("end"):
                return
            yield FileEntry.from_dict(d)


def is_complete(path: str) -> bool:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - 200))
        tail = f.read().splitlines()
    return bool(tail) and b'"end"' in tail[-1]


def reopen(path: str) -> tuple[BlockSource, Filesystem, SessionHeader]:
    """Open the original source and the volume a session was made from (no scan). Fails if the source changed size."""
    h = read_header(path)
    src = open_source(h.source)
    if src.size != h.source_size:
        src.close()
        raise FsError(f"{h.source} is {src.size} bytes now but the session was made from {h.source_size}; refusing to resume")
    vol: BlockSource = PartitionView(src, h.volume_offset, h.volume_size) if (h.volume_offset or h.volume_size != src.size) else src
    return vol, open_filesystem(vol), h
