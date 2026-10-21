"""Disk and volume enumeration (metadata only; never opens a disk for writing)."""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict, dataclass, field


@dataclass
class VolumeInfo:
    label: str = ""
    mountpoint: str = ""  # drive letter ("C:") or Linux mount point
    fs: str = ""
    size: int = 0
    partition_offset: int | None = None


@dataclass
class DiskInfo:
    path: str  # \\.\PhysicalDrive0 or /dev/sda
    model: str = ""
    size: int = 0
    style: str = ""  # MBR / GPT / RAW
    is_system: bool = False
    removable: bool = False
    volumes: list[VolumeInfo] = field(default_factory=list)

    def mountpoints(self) -> set[str]:
        return {v.mountpoint.rstrip("\\/").upper() for v in self.volumes if v.mountpoint}

    def to_dict(self) -> dict:
        return asdict(self)


def _run(cmd: list[str], timeout: int = 30) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False).stdout


_PS = r"""
$out = @()
foreach ($d in Get-Disk) {
  $vols = @()
  foreach ($p in (Get-Partition -DiskNumber $d.Number -ErrorAction SilentlyContinue)) {
    $v = $null
    if ($p.DriveLetter) { $v = Get-Volume -DriveLetter $p.DriveLetter -ErrorAction SilentlyContinue }
    $vols += [pscustomobject]@{ label = "$($v.FileSystemLabel)"; mountpoint = $(if ($p.DriveLetter) { "$($p.DriveLetter):" } else { "" });
      fs = "$($v.FileSystem)"; size = [int64]$p.Size; partition_offset = [int64]$p.Offset }
  }
  $out += [pscustomobject]@{ path = "\\.\PhysicalDrive$($d.Number)"; model = "$($d.FriendlyName)"; size = [int64]$d.Size;
    style = "$($d.PartitionStyle)"; is_system = [bool]($d.IsSystem -or $d.IsBoot); removable = ($d.BusType -eq 'USB'); volumes = $vols }
}
ConvertTo-Json -InputObject $out -Depth 4
"""


def _from_json(items: list[dict]) -> list[DiskInfo]:
    disks = []
    for it in items:
        vols = [VolumeInfo(**v) for v in it.pop("volumes", None) or []]
        disks.append(DiskInfo(volumes=vols, **it))
    return disks


def list_disks() -> list[DiskInfo]:
    if sys.platform == "win32":
        raw = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS], timeout=60)
        if not raw.strip():
            return []
        data = json.loads(raw)
        return _from_json(data if isinstance(data, list) else [data])
    raw = _run(["lsblk", "-J", "-b", "-o", "NAME,PATH,MODEL,SIZE,TYPE,PTTYPE,RM,MOUNTPOINT,FSTYPE,LABEL,FSROOTS"])
    if not raw.strip():
        return []
    disks = []
    for d in json.loads(raw).get("blockdevices", []):
        if d.get("type") != "disk":
            continue
        vols = [VolumeInfo(label=c.get("label") or "", mountpoint=c.get("mountpoint") or "", fs=c.get("fstype") or "",
                           size=int(c.get("size") or 0)) for c in d.get("children", [])]
        disks.append(DiskInfo(path=d["path"], model=(d.get("model") or "").strip(), size=int(d.get("size") or 0),
                              style=(d.get("pttype") or "RAW").upper(), removable=bool(d.get("rm")),
                              is_system=any(v.mountpoint in ("/", "/boot") for v in vols), volumes=vols))
    return disks


def disk_for_path(device_path: str) -> DiskInfo | None:
    for d in list_disks():
        if d.path.lower() == device_path.lower():
            return d
    return None
