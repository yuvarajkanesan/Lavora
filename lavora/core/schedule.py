"""Scheduled backups: Task Scheduler on Windows, systemd timers on Linux. Generation is pure; installing is opt-in."""
from __future__ import annotations

import subprocess
import sys


def windows_task_command(task_name: str, exe: str, args: str, time_hhmm: str = "02:00", frequency: str = "DAILY") -> list[str]:
    if frequency.upper() not in ("DAILY", "WEEKLY", "HOURLY"):
        raise ValueError("frequency must be DAILY, WEEKLY or HOURLY")
    return ["schtasks", "/Create", "/F", "/TN", f"Lavora\\{task_name}", "/TR", f'"{exe}" {args}', "/SC", frequency.upper(),
            "/ST", time_hhmm, "/RL", "HIGHEST"]


def systemd_units(name: str, exe: str, args: str, on_calendar: str = "daily") -> dict[str, str]:
    service = f"""[Unit]
Description=Lavora backup ({name})

[Service]
Type=oneshot
ExecStart={exe} {args}
"""
    timer = f"""[Unit]
Description=Run Lavora backup ({name}) on a schedule

[Timer]
OnCalendar={on_calendar}
Persistent=true

[Install]
WantedBy=timers.target
"""
    return {f"lavora-{name}.service": service, f"lavora-{name}.timer": timer}


def install_windows_task(cmd: list[str]) -> None:
    if sys.platform != "win32":
        raise OSError("Task Scheduler is Windows only")
    subprocess.run(cmd, check=True, capture_output=True, text=True)
