from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

_DOW = {
    "mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu",
    "fri": "Fri", "sat": "Sat", "sun": "Sun",
}


@dataclass
class Schedule:
    oncalendar: str
    human: str


def _check_time(hh: str, mm: str) -> None:
    h, m = int(hh), int(mm)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError("time out of range: %s:%s" % (hh, mm))


def parse_schedule(spec: str) -> Schedule:
    spec = spec.strip()

    if spec == "hourly":
        return Schedule("hourly", "every hour")

    m = re.fullmatch(r"daily@(\d{2}):(\d{2})", spec)
    if m:
        hh, mm = m.group(1), m.group(2)
        _check_time(hh, mm)
        return Schedule("*-*-* %s:%s:00" % (hh, mm), "daily at %s:%s" % (hh, mm))

    m = re.fullmatch(r"weekly@([a-z]{3}):(\d{2}):(\d{2})", spec)
    if m:
        dow, hh, mm = m.group(1), m.group(2), m.group(3)
        if dow not in _DOW:
            raise ValueError("unknown weekday: %s" % dow)
        _check_time(hh, mm)
        return Schedule(
            "%s *-*-* %s:%s:00" % (_DOW[dow], hh, mm),
            "weekly on %s at %s:%s" % (_DOW[dow], hh, mm),
        )

    m = re.fullmatch(r"every:(\d+)h", spec)
    if m:
        n = int(m.group(1))
        if not (1 <= n <= 23):
            raise ValueError("hours must be 1-23: %s" % n)
        return Schedule("*-*-* 00/%d:00:00" % n, "every %d hour(s)" % n)

    m = re.fullmatch(r"every:(\d+)m", spec)
    if m:
        n = int(m.group(1))
        if not (1 <= n <= 59):
            raise ValueError("minutes must be 1-59: %s" % n)
        return Schedule("*-*-* *:00/%d" % n, "every %d minute(s)" % n)

    raise ValueError(
        "unrecognized schedule %r; use hourly | daily@HH:MM | "
        "weekly@dow:HH:MM | every:Nh | every:Nm" % spec
    )


def validate_oncalendar(expr: str) -> bool:
    try:
        result = subprocess.run(
            ["systemd-analyze", "calendar", expr],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        return True  # systemd-analyze unavailable; trust the expression
    return result.returncode == 0
