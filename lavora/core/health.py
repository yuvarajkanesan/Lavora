"""Disk health check: a read-only surface scan (speed, slow areas, unreadable areas) plus SMART where the drive reports it."""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .device import BlockSource
from .smart import SmartReport, read_smart, surface_scan


@dataclass
class HealthReport:
    scanned: int = 0
    total: int = 0
    seconds: float = 0.0
    unreadable: list[tuple[int, int]] = field(default_factory=list)
    slow: list[tuple[int, float]] = field(default_factory=list)  # (offset, seconds for one block)
    smart: SmartReport | None = None
    cancelled: bool = False

    @property
    def speed(self) -> float:
        """Average read speed in bytes/s over the whole scan."""
        return self.scanned / self.seconds if self.seconds > 0 else 0.0

    @property
    def verdict(self) -> str:
        if self.unreadable or (self.smart and self.smart.status == "critical"):
            return "failing"
        if self.slow or (self.smart and self.smart.status == "warning"):
            return "slow"
        return "healthy"

    def advice(self) -> str:
        v = self.verdict
        if v == "failing":
            return "Unreadable areas were found. Stop using this disk and image it now (Disk is failing), then recover from the image."
        if v == "slow":
            return "Some areas read very slowly. The disk may be starting to fail: back up or image it soon."
        return "No unreadable or slow areas were found in the part that was scanned."

    def summary_text(self) -> str:
        gb = 1e9
        lines = [f"scanned {self.scanned / gb:.1f} GB of {self.total / gb:.1f} GB in {self.seconds:.0f} s (average {self.speed / 1e6:.1f} MB/s)",
                 f"unreadable blocks: {len(self.unreadable)}   slow blocks: {len(self.slow)}"]
        if self.smart and self.smart.available:
            lines.append(f"SMART: {self.smart.status} (score {self.smart.score}/100)" + "".join(f"\n  ! {w}" for w in self.smart.warnings))
        elif self.smart and self.smart.detail:
            lines.append(f"SMART: {self.smart.detail}")
        lines.append(f"verdict: {self.verdict}. {self.advice()}")
        return "\n".join(lines)


def check_health(source: BlockSource, *, max_bytes: int | None = None, block: int = 1 << 20, slow_after: float = 0.5,
                 smart_device: str | None = None, progress: Callable[[int, int], None] | None = None,
                 cancel: Callable[[], bool] | None = None) -> HealthReport:
    """Read the first `max_bytes` (or all) of `source` once, read-only, timing every block."""
    t = time.perf_counter()
    res = surface_scan(source, block=block, slow_after=slow_after, progress=progress, max_bytes=max_bytes, cancel=cancel)
    rep = HealthReport(scanned=res.scanned, total=min(source.size, max_bytes or source.size), seconds=time.perf_counter() - t,
                       unreadable=list(res.bad), slow=list(res.slow), cancelled=bool(cancel and cancel()))
    if smart_device:
        rep.smart = read_smart(smart_device)
    return rep
