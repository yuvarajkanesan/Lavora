"""Which parts of a (partial) disk image were never read? Reads the imaging map file that sits next to the image.

A cancelled or stuck imaging run leaves zeros where the source was not copied. Files whose data falls there would silently
recover as zeros, so recovery uses these ranges to flag them as incomplete.
"""
from __future__ import annotations

import bisect
import json
import os

Range = tuple[int, int]  # (offset, length)


def load_unread(image_path: str) -> list[Range] | None:
    """Unread/unreliable ranges of an image, merged and sorted; None if the image has no imaging map (assume complete).

    Unread = skipped or failed blocks (suspect), bad sectors, and everything after the copy position if pass 1 never finished."""
    mapfile = image_path + ".map"
    if not os.path.isfile(mapfile):
        return None
    try:
        with open(mapfile, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        return None
    ranges = [tuple(r) for r in st.get("suspect", [])] + [tuple(r) for r in st.get("bad", [])]
    size, nxt = st.get("size", 0), st.get("next", 0)
    if st.get("pass", 1) == 1 and nxt < size:
        ranges.append((nxt, size - nxt))
    return merge([(int(o), int(n)) for o, n in ranges if n > 0])


def merge(ranges: list[Range]) -> list[Range]:
    out: list[Range] = []
    for off, ln in sorted(ranges):
        if out and off <= out[-1][0] + out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][0] + out[-1][1], off + ln) - out[-1][0])
        else:
            out.append((off, ln))
    return out


def unread_total(unread: list[Range]) -> int:
    return sum(n for _o, n in unread)


def missing_percent(file_ranges: list[Range], unread: list[Range], base: int = 0) -> float:
    """Share (0..100) of a file's bytes that lie in unread ranges. `base` converts volume offsets to image offsets."""
    total = sum(n for _o, n in file_ranges)
    if not total or not unread:
        return 0.0
    starts = [o for o, _n in unread]
    missing = 0
    for off, ln in file_ranges:
        lo, hi = off + base, off + base + ln
        i = max(0, bisect.bisect_right(starts, lo) - 1)
        while i < len(unread) and unread[i][0] < hi:
            u0, un = unread[i]
            missing += max(0, min(hi, u0 + un) - max(lo, u0))
            i += 1
    return 100.0 * missing / total
