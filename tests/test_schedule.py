from __future__ import annotations

import shutil

import pytest

from backup.schedule import parse_schedule, validate_oncalendar


def test_hourly():
    s = parse_schedule("hourly")
    assert s.oncalendar == "hourly"


def test_daily_at():
    s = parse_schedule("daily@02:00")
    assert s.oncalendar == "*-*-* 02:00:00"
    assert "02:00" in s.human


def test_weekly_at():
    s = parse_schedule("weekly@sun:03:30")
    assert s.oncalendar == "Sun *-*-* 03:30:00"


def test_every_hours():
    s = parse_schedule("every:6h")
    assert s.oncalendar == "*-*-* 00/6:00:00"


def test_every_minutes():
    s = parse_schedule("every:30m")
    assert s.oncalendar == "*-*-* *:00/30"


@pytest.mark.parametrize("bad", [
    "daily@25:00", "weekly@xday:01:00", "every:0h", "every:6x", "nonsense", "daily",
])
def test_malformed_rejected(bad):
    with pytest.raises(ValueError):
        parse_schedule(bad)


@pytest.mark.skipif(
    shutil.which("systemd-analyze") is None, reason="systemd-analyze not available"
)
def test_generated_expressions_are_valid():
    for spec in ["hourly", "daily@02:00", "weekly@sun:03:30", "every:6h", "every:30m"]:
        assert validate_oncalendar(parse_schedule(spec).oncalendar)
