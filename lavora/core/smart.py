"""SMART health (via smartmontools' smartctl, bundled as a separate executable) and a read-only surface scan."""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field

from .device import BlockSource

CRITICAL_ATTRS = {5: "Reallocated sectors", 187: "Reported uncorrectable errors", 188: "Command timeouts",
                  196: "Reallocation events", 197: "Pending sectors", 198: "Offline uncorrectable sectors"}


@dataclass
class SmartReport:
    available: bool = False
    model: str = ""
    passed: bool | None = None
    temperature_c: int | None = None
    power_on_hours: int | None = None
    score: int = 100  # 0 (failing) .. 100 (healthy)
    warnings: list[str] = field(default_factory=list)
    detail: str = ""

    @property
    def status(self) -> str:
        if not self.available:
            return "unknown"
        return "critical" if self.score < 50 or self.passed is False else "warning" if self.score < 85 else "good"


def find_smartctl() -> str | None:
    return shutil.which("smartctl")


def parse_smart(data: dict) -> SmartReport:
    """Turn `smartctl -j -a` output into a health score. Pure function (testable with captured JSON)."""
    r = SmartReport(available=True, model=data.get("model_name", ""))
    r.passed = data.get("smart_status", {}).get("passed")
    r.temperature_c = data.get("temperature", {}).get("current")
    r.power_on_hours = data.get("power_on_time", {}).get("hours")
    if r.passed is False:
        r.score = min(r.score, 10)
        r.warnings.append("The drive reports that SMART health has FAILED. Back up immediately.")
    for a in data.get("ata_smart_attributes", {}).get("table", []):
        raw = a.get("raw", {}).get("value", 0)
        if a.get("id") in CRITICAL_ATTRS and raw:
            r.score -= min(40, 10 + raw // 10)
            r.warnings.append(f"{CRITICAL_ATTRS[a['id']]}: {raw}")
        if a.get("when_failed") not in (None, "", "-"):
            r.score -= 30
            r.warnings.append(f"Attribute {a.get('name')} failed ({a['when_failed']}).")
    nvme = data.get("nvme_smart_health_information_log", {})
    if nvme:
        if nvme.get("critical_warning"):
            r.score -= 40
            r.warnings.append(f"NVMe critical warning flags: {nvme['critical_warning']}")
        used = nvme.get("percentage_used", 0)
        if used >= 90:
            r.score -= 30
            r.warnings.append(f"NVMe wear: {used}% of rated endurance used")
        if nvme.get("media_errors"):
            r.score -= 20
            r.warnings.append(f"Media errors: {nvme['media_errors']}")
        r.temperature_c = r.temperature_c or nvme.get("temperature")
    if r.temperature_c and r.temperature_c >= 60:
        r.score -= 10
        r.warnings.append(f"Running hot: {r.temperature_c} C")
    r.score = max(0, min(100, r.score))
    return r


def read_smart(device: str) -> SmartReport:
    exe = find_smartctl()
    if not exe:
        return SmartReport(detail="smartctl (smartmontools) not found; install it or use the bundled copy.")
    try:
        proc = subprocess.run([exe, "-j", "-a", device], capture_output=True, text=True, timeout=60, check=False)
        return parse_smart(json.loads(proc.stdout))
    except (OSError, ValueError, subprocess.SubprocessError) as ex:
        return SmartReport(detail=f"smartctl failed: {ex}")


@dataclass
class SurfaceResult:
    scanned: int = 0
    bad: list[tuple[int, int]] = field(default_factory=list)
    slow: list[tuple[int, float]] = field(default_factory=list)  # (offset, seconds)

    @property
    def healthy(self) -> bool:
        return not self.bad


def surface_scan(source: BlockSource, *, block: int = 1 << 20, slow_after: float = 0.5, progress=None,
                 max_bytes: int | None = None, cancel=None, start: int = 0) -> SurfaceResult:
    """Read the whole source once, recording unreadable blocks and unusually slow ones. Read-only."""
    res = SurfaceResult()
    limit = min(source.size, max_bytes or source.size)
    pos = start
    while pos < limit:
        if cancel and cancel():
            break
        n = min(block, limit - pos)
        t = time.perf_counter()
        try:
            source.read_exact(pos, n)
        except OSError:
            res.bad.append((pos, n))
        dt = time.perf_counter() - t
        if dt > slow_after:
            res.slow.append((pos, dt))
        pos += n
        res.scanned = pos
        if progress:
            progress(pos, limit)
    return res
