# `backup` CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `backup`, a per-user Python CLI that registers directories as recurring backup jobs, each driven by a systemd user timer that takes rsync hard-link snapshots into a local destination with keep-last-N retention.

**Architecture:** A stdlib-only Python package under `src/backup/`. SQLite (`jobs.db`) is the durable source of truth for job metadata; systemd user timers/services are generated per job and queried live for active/next-run state. The CLI wires together six focused modules: `paths` (dir policy), `db` (SQLite CRUD), `schedule` (preset→OnCalendar), `units` (systemd unit rendering + systemctl wrapper), `runner` (rsync snapshot + prune), and `cli` (argparse dispatch). Bash `install.sh`/`uninstall.sh` install it per-user and enable linger.

**Tech Stack:** Python 3.9 (stdlib only — `argparse`, `sqlite3`, `dataclasses`, `pathlib`, `subprocess`), `rsync`, `systemctl --user`, `systemd-analyze`, `pytest` for tests, Bash for install.

## Global Constraints

- Python **3.9** compatible — no `match`, no PEP 604 `X | Y` runtime unions; put `from __future__ import annotations` at the top of every module and use `typing.Optional` / `typing.List`.
- **Stdlib only** in `src/backup/` — no third-party runtime dependencies. `pytest` is a dev/test dependency only.
- **Per-user** only: paths resolve under `$XDG_CONFIG_HOME`/`$XDG_STATE_HOME` (fallback `~/.config`, `~/.local/state`); systemd units under `$XDG_CONFIG_HOME/systemd/user`. Never write to system dirs or require root.
- Command name is **`backup`**; internal run subcommand is **`_run`**.
- Default retention **keep = 7**.
- rsync success = exit code **0 or 24**; any other code is a failure.
- Tests must not touch the real systemd: the `systemctl`/`systemd-analyze` calls live behind `units._systemctl` and `schedule.validate_oncalendar`, which tests monkeypatch. Tests set `XDG_CONFIG_HOME`/`XDG_STATE_HOME` to a tmp dir.
- All filesystem paths stored and compared as **absolute, resolved** paths.

---

## File Structure

```
backup-tool/
  pyproject.toml                 # metadata + console_scripts: backup = backup.cli:main
  src/backup/__init__.py
  src/backup/paths.py            # XDG-aware dir/path resolution
  src/backup/db.py               # Job dataclass + SQLite schema/CRUD
  src/backup/schedule.py         # parse presets -> OnCalendar; validate
  src/backup/units.py            # render service/timer; systemctl wrapper
  src/backup/runner.py           # rsync snapshot, finalize, latest, prune
  src/backup/cli.py              # argparse dispatch + validation
  tests/conftest.py              # tmp XDG env fixture
  tests/test_paths.py
  tests/test_db.py
  tests/test_schedule.py
  tests/test_units.py
  tests/test_runner.py
  tests/test_cli.py
  install.sh
  uninstall.sh
  README.md
  LICENSE
```

---

## Task 1: Project scaffold + `paths.py`

**Files:**
- Create: `pyproject.toml`, `src/backup/__init__.py`, `src/backup/paths.py`
- Create: `tests/conftest.py`, `tests/test_paths.py`

**Interfaces:**
- Consumes: nothing.
- Produces (`backup.paths`):
  - `config_dir() -> Path` — `$XDG_CONFIG_HOME/backup` or `~/.config/backup`
  - `state_dir() -> Path` — `$XDG_STATE_HOME/backup` or `~/.local/state/backup`
  - `log_dir() -> Path` — `state_dir()/logs`
  - `db_path() -> Path` — `config_dir()/jobs.db`
  - `systemd_user_dir() -> Path` — `$XDG_CONFIG_HOME/systemd/user` or `~/.config/systemd/user`
  - `ensure_dirs() -> None` — creates config/state/log/systemd dirs
  - `backup_executable() -> str` — absolute path to the `backup` entry point (`shutil.which('backup')` else resolved `sys.argv[0]`)

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "backup-cli"
version = "0.1.0"
description = "Per-directory rsync snapshot backups driven by systemd user timers"
requires-python = ">=3.9"
dependencies = []

[project.scripts]
backup = "backup.cli:main"

[project.optional-dependencies]
dev = ["pytest>=7"]

[tool.setuptools]
package-dir = {"" = "src"}

[tool.setuptools.packages.find]
where = ["src"]
```

- [ ] **Step 2: Create `src/backup/__init__.py`**

```python
"""backup: per-directory rsync snapshot backups via systemd user timers."""

__version__ = "0.1.0"
```

- [ ] **Step 3: Create `tests/conftest.py`** (shared tmp-XDG fixture)

```python
from __future__ import annotations

import importlib
import pytest


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    """Point all XDG dirs at a tmp dir and return the config/state roots."""
    cfg = tmp_path / "config"
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    import backup.paths as paths
    importlib.reload(paths)
    paths.ensure_dirs()
    return {"config": cfg / "backup", "state": state / "backup", "paths": paths}
```

- [ ] **Step 4: Write the failing test** — `tests/test_paths.py`

```python
from __future__ import annotations

from pathlib import Path


def test_dirs_honor_xdg(xdg):
    paths = xdg["paths"]
    assert paths.config_dir() == xdg["config"]
    assert paths.state_dir() == xdg["state"]
    assert paths.log_dir() == xdg["state"] / "logs"
    assert paths.db_path() == xdg["config"] / "jobs.db"
    assert paths.systemd_user_dir().name == "user"


def test_ensure_dirs_creates_everything(xdg):
    paths = xdg["paths"]
    assert paths.config_dir().is_dir()
    assert paths.log_dir().is_dir()
    assert paths.systemd_user_dir().is_dir()


def test_backup_executable_is_absolute(xdg):
    exe = xdg["paths"].backup_executable()
    assert Path(exe).is_absolute()
```

- [ ] **Step 5: Run test to verify it fails**

Run: `cd /home/william/backup-tool && PYTHONPATH=src python -m pytest tests/test_paths.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backup.paths'`

- [ ] **Step 6: Write `src/backup/paths.py`**

```python
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _xdg(env: str, default: Path) -> Path:
    value = os.environ.get(env)
    return Path(value) if value else default


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", Path.home() / ".config") / "backup"


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", Path.home() / ".local" / "state") / "backup"


def log_dir() -> Path:
    return state_dir() / "logs"


def db_path() -> Path:
    return config_dir() / "jobs.db"


def systemd_user_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", Path.home() / ".config") / "systemd" / "user"


def ensure_dirs() -> None:
    for d in (config_dir(), state_dir(), log_dir(), systemd_user_dir()):
        d.mkdir(parents=True, exist_ok=True)


def backup_executable() -> str:
    found = shutil.which("backup")
    if found:
        return str(Path(found).resolve())
    return str(Path(sys.argv[0]).resolve())
```

- [ ] **Step 7: Run test to verify it passes**

Run: `PYTHONPATH=src python -m pytest tests/test_paths.py -v`
Expected: PASS (3 passed)

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml src/backup/__init__.py src/backup/paths.py tests/conftest.py tests/test_paths.py
git commit -m "feat: project scaffold and XDG-aware paths module"
```

---

## Task 2: `db.py` — Job model + SQLite CRUD

**Files:**
- Create: `src/backup/db.py`, `tests/test_db.py`

**Interfaces:**
- Consumes (`backup.paths`): `db_path()`, `ensure_dirs()`.
- Produces (`backup.db`):
  - `@dataclass Job` fields: `name: str, source: str, dest: str, oncalendar: str, schedule_human: str, keep: int, created_at: str, last_run_at: Optional[str]=None, last_status: Optional[str]=None, last_message: Optional[str]=None`
  - `connect(path: Optional[Path]=None) -> sqlite3.Connection` — opens DB (default `paths.db_path()`), creates schema if missing, `row_factory = sqlite3.Row`
  - `add_job(conn, job: Job) -> None` — raises `ValueError` if name or source already exists
  - `get_job(conn, name: str) -> Optional[Job]`
  - `get_job_by_source(conn, source: str) -> Optional[Job]`
  - `list_jobs(conn) -> List[Job]` — ordered by name
  - `update_job(conn, name: str, **fields) -> None` — updates given columns (raises `ValueError` on unknown column or rename collision)
  - `remove_job(conn, name: str) -> bool` — returns True if a row was deleted
  - `record_run(conn, name: str, status: str, message: str, run_at: str) -> None`

- [ ] **Step 1: Write the failing test** — `tests/test_db.py`

```python
from __future__ import annotations

import pytest

from backup.db import (
    Job,
    add_job,
    connect,
    get_job,
    get_job_by_source,
    list_jobs,
    record_run,
    remove_job,
    update_job,
)


def make_job(name="docs", source="/a/docs", dest="/b"):
    return Job(
        name=name,
        source=source,
        dest=dest,
        oncalendar="*-*-* 02:00:00",
        schedule_human="daily at 02:00",
        keep=7,
        created_at="2026-06-28T00:00:00",
    )


def test_add_and_get(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, make_job())
    job = get_job(conn, "docs")
    assert job is not None
    assert job.source == "/a/docs"
    assert job.keep == 7


def test_duplicate_name_rejected(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, make_job())
    with pytest.raises(ValueError):
        add_job(conn, make_job(source="/a/other"))


def test_duplicate_source_rejected(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, make_job())
    with pytest.raises(ValueError):
        add_job(conn, make_job(name="other"))


def test_get_by_source(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, make_job())
    assert get_job_by_source(conn, "/a/docs").name == "docs"
    assert get_job_by_source(conn, "/nope") is None


def test_list_ordered(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, make_job(name="zeta", source="/z"))
    add_job(conn, make_job(name="alpha", source="/a"))
    assert [j.name for j in list_jobs(conn)] == ["alpha", "zeta"]


def test_update_and_rename(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, make_job())
    update_job(conn, "docs", keep=3, name="documents")
    assert get_job(conn, "docs") is None
    assert get_job(conn, "documents").keep == 3


def test_record_run(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, make_job())
    record_run(conn, "docs", "ok", "1 snapshot", "2026-06-28T02:00:00")
    job = get_job(conn, "docs")
    assert job.last_status == "ok"
    assert job.last_run_at == "2026-06-28T02:00:00"


def test_remove(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, make_job())
    assert remove_job(conn, "docs") is True
    assert remove_job(conn, "docs") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src python -m pytest tests/test_db.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backup.db'`

- [ ] **Step 3: Write `src/backup/db.py`**

```python
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, fields
from pathlib import Path
from typing import List, Optional

from . import paths

_COLUMNS = (
    "name", "source", "dest", "oncalendar", "schedule_human",
    "keep", "created_at", "last_run_at", "last_status", "last_message",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    name           TEXT PRIMARY KEY,
    source         TEXT NOT NULL UNIQUE,
    dest           TEXT NOT NULL,
    oncalendar     TEXT NOT NULL,
    schedule_human TEXT NOT NULL,
    keep           INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    last_run_at    TEXT,
    last_status    TEXT,
    last_message   TEXT
);
"""


@dataclass
class Job:
    name: str
    source: str
    dest: str
    oncalendar: str
    schedule_human: str
    keep: int
    created_at: str
    last_run_at: Optional[str] = None
    last_status: Optional[str] = None
    last_message: Optional[str] = None


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    if path is None:
        paths.ensure_dirs()
        path = paths.db_path()
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(**{f.name: row[f.name] for f in fields(Job)})


def add_job(conn: sqlite3.Connection, job: Job) -> None:
    try:
        conn.execute(
            "INSERT INTO jobs (name, source, dest, oncalendar, schedule_human, "
            "keep, created_at, last_run_at, last_status, last_message) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                job.name, job.source, job.dest, job.oncalendar,
                job.schedule_human, job.keep, job.created_at,
                job.last_run_at, job.last_status, job.last_message,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError(str(exc)) from exc
    conn.commit()


def get_job(conn: sqlite3.Connection, name: str) -> Optional[Job]:
    row = conn.execute("SELECT * FROM jobs WHERE name = ?", (name,)).fetchone()
    return _row_to_job(row) if row else None


def get_job_by_source(conn: sqlite3.Connection, source: str) -> Optional[Job]:
    row = conn.execute("SELECT * FROM jobs WHERE source = ?", (source,)).fetchone()
    return _row_to_job(row) if row else None


def list_jobs(conn: sqlite3.Connection) -> List[Job]:
    rows = conn.execute("SELECT * FROM jobs ORDER BY name").fetchall()
    return [_row_to_job(r) for r in rows]


def update_job(conn: sqlite3.Connection, name: str, **fields_: object) -> None:
    if not fields_:
        return
    unknown = set(fields_) - set(_COLUMNS)
    if unknown:
        raise ValueError("unknown column(s): %s" % ", ".join(sorted(unknown)))
    assignments = ", ".join("%s = ?" % col for col in fields_)
    try:
        conn.execute(
            "UPDATE jobs SET %s WHERE name = ?" % assignments,
            (*fields_.values(), name),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError(str(exc)) from exc
    conn.commit()


def remove_job(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute("DELETE FROM jobs WHERE name = ?", (name,))
    conn.commit()
    return cur.rowcount > 0


def record_run(
    conn: sqlite3.Connection, name: str, status: str, message: str, run_at: str
) -> None:
    update_job(
        conn, name,
        last_status=status, last_message=message, last_run_at=run_at,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src python -m pytest tests/test_db.py -v`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add src/backup/db.py tests/test_db.py
git commit -m "feat: SQLite job registry with CRUD"
```

---

## Task 3: `schedule.py` — presets → OnCalendar

**Files:**
- Create: `src/backup/schedule.py`, `tests/test_schedule.py`

**Interfaces:**
- Consumes: nothing.
- Produces (`backup.schedule`):
  - `@dataclass Schedule` fields: `oncalendar: str, human: str`
  - `parse_schedule(spec: str) -> Schedule` — raises `ValueError` on malformed preset
  - `validate_oncalendar(expr: str) -> bool` — runs `systemd-analyze calendar <expr>`, returns True if rc==0 (tests monkeypatch this)

- [ ] **Step 1: Write the failing test** — `tests/test_schedule.py`

```python
from __future__ import annotations

import pytest

from backup.schedule import parse_schedule


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src python -m pytest tests/test_schedule.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backup.schedule'`

- [ ] **Step 3: Write `src/backup/schedule.py`**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src python -m pytest tests/test_schedule.py -v`
Expected: PASS (10 passed)

- [ ] **Step 5: Add an integration check that real OnCalendar values validate** — append to `tests/test_schedule.py`

```python
import shutil

from backup.schedule import validate_oncalendar


@pytest.mark.skipif(
    shutil.which("systemd-analyze") is None, reason="systemd-analyze not available"
)
def test_generated_expressions_are_valid():
    for spec in ["hourly", "daily@02:00", "weekly@sun:03:30", "every:6h", "every:30m"]:
        assert validate_oncalendar(parse_schedule(spec).oncalendar)
```

- [ ] **Step 6: Run it**

Run: `PYTHONPATH=src python -m pytest tests/test_schedule.py -v`
Expected: PASS (11 passed)

- [ ] **Step 7: Commit**

```bash
git add src/backup/schedule.py tests/test_schedule.py
git commit -m "feat: schedule preset parsing to systemd OnCalendar"
```

---

## Task 4: `units.py` — systemd unit rendering + systemctl wrapper

**Files:**
- Create: `src/backup/units.py`, `tests/test_units.py`

**Interfaces:**
- Consumes (`backup.paths`): `systemd_user_dir()`.
- Produces (`backup.units`):
  - `render_service(name: str, exec_path: str, source: str) -> str`
  - `render_timer(name: str, oncalendar: str) -> str`
  - `unit_paths(name: str) -> Tuple[Path, Path]` — (service file, timer file)
  - `_systemctl(*args: str) -> subprocess.CompletedProcess` — runs `systemctl --user <args>` (tests monkeypatch this)
  - `install_units(name, oncalendar, exec_path, source) -> None` — write both files, `daemon-reload`, `enable --now <timer>`
  - `remove_units(name) -> None` — `disable --now`, delete files, `daemon-reload`
  - `pause_units(name) -> None` — `disable --now <timer>`
  - `resume_units(name) -> None` — `enable --now <timer>`
  - `run_now(name) -> None` — `start <service>`
  - `is_active(name) -> bool` — `is-enabled <timer>` rc==0
  - `next_run(name) -> Optional[str]` — parse `list-timers` for the timer's next run

- [ ] **Step 1: Write the failing test** — `tests/test_units.py`

```python
from __future__ import annotations

import backup.units as units


def test_render_service_contains_run_command(xdg):
    text = units.render_service("docs", "/home/u/.local/bin/backup", "/a/docs")
    assert "ExecStart=/home/u/.local/bin/backup _run docs" in text
    assert "Type=oneshot" in text


def test_render_timer_contains_oncalendar(xdg):
    text = units.render_timer("docs", "*-*-* 02:00:00")
    assert "OnCalendar=*-*-* 02:00:00" in text
    assert "Persistent=true" in text


def test_install_units_writes_files_and_calls_systemctl(xdg, monkeypatch):
    calls = []
    monkeypatch.setattr(units, "_systemctl", lambda *a: calls.append(a) or _ok())
    units.install_units("docs", "*-*-* 02:00:00", "/bin/backup", "/a/docs")
    svc, timer = units.unit_paths("docs")
    assert svc.exists() and timer.exists()
    assert ("daemon-reload",) in calls
    assert ("enable", "--now", "backup-docs.timer") in calls


def test_remove_units_deletes_files(xdg, monkeypatch):
    monkeypatch.setattr(units, "_systemctl", lambda *a: _ok())
    units.install_units("docs", "*-*-* 02:00:00", "/bin/backup", "/a/docs")
    units.remove_units("docs")
    svc, timer = units.unit_paths("docs")
    assert not svc.exists() and not timer.exists()


class _CP:
    def __init__(self, rc):
        self.returncode = rc
        self.stdout = ""


def _ok():
    return _CP(0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src python -m pytest tests/test_units.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backup.units'`

- [ ] **Step 3: Write `src/backup/units.py`**

```python
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional, Tuple

from . import paths

SERVICE_TEMPLATE = """\
[Unit]
Description=backup snapshot for {name}
After=network.target

[Service]
Type=oneshot
ExecStart={exec_path} _run {name}
WorkingDirectory={source}
"""

TIMER_TEMPLATE = """\
[Unit]
Description=backup timer for {name}

[Timer]
OnCalendar={oncalendar}
Persistent=true

[Install]
WantedBy=timers.target
"""


def _timer_unit(name: str) -> str:
    return "backup-%s.timer" % name


def _service_unit(name: str) -> str:
    return "backup-%s.service" % name


def unit_paths(name: str) -> Tuple[Path, Path]:
    d = paths.systemd_user_dir()
    return d / _service_unit(name), d / _timer_unit(name)


def render_service(name: str, exec_path: str, source: str) -> str:
    return SERVICE_TEMPLATE.format(name=name, exec_path=exec_path, source=source)


def render_timer(name: str, oncalendar: str) -> str:
    return TIMER_TEMPLATE.format(name=name, oncalendar=oncalendar)


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True,
    )


def install_units(name: str, oncalendar: str, exec_path: str, source: str) -> None:
    paths.systemd_user_dir().mkdir(parents=True, exist_ok=True)
    svc, timer = unit_paths(name)
    svc.write_text(render_service(name, exec_path, source))
    timer.write_text(render_timer(name, oncalendar))
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", _timer_unit(name))


def remove_units(name: str) -> None:
    _systemctl("disable", "--now", _timer_unit(name))
    svc, timer = unit_paths(name)
    for p in (svc, timer):
        if p.exists():
            p.unlink()
    _systemctl("daemon-reload")


def pause_units(name: str) -> None:
    _systemctl("disable", "--now", _timer_unit(name))


def resume_units(name: str) -> None:
    _systemctl("enable", "--now", _timer_unit(name))


def run_now(name: str) -> None:
    _systemctl("start", _service_unit(name))


def is_active(name: str) -> bool:
    return _systemctl("is-enabled", _timer_unit(name)).returncode == 0


def next_run(name: str) -> Optional[str]:
    result = _systemctl("list-timers", "--all", "--no-pager", _timer_unit(name))
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if _timer_unit(name) in line:
            return line.strip()
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src python -m pytest tests/test_units.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/backup/units.py tests/test_units.py
git commit -m "feat: systemd unit rendering and systemctl wrapper"
```

---

## Task 5: `runner.py` — rsync snapshot + prune

**Files:**
- Create: `src/backup/runner.py`, `tests/test_runner.py`

**Interfaces:**
- Consumes (`backup.db`): `Job`, `record_run`. (`backup.paths`): `log_dir`.
- Produces (`backup.runner`):
  - `@dataclass RunResult` fields: `status: str, message: str, snapshot: Optional[str]`
  - `job_dir(job: Job) -> Path` — `Path(job.dest)/job.name`
  - `list_snapshots(job: Job) -> List[Path]` — finalized snapshot dirs (exclude `*.partial`), sorted oldest→newest
  - `run_backup(job: Job, conn=None, now: Optional[datetime]=None) -> RunResult` — performs snapshot, finalize, `latest` symlink, prune to `job.keep`, and (if `conn`) `record_run`

- [ ] **Step 1: Write the failing test** — `tests/test_runner.py`

```python
from __future__ import annotations

import os
import shutil
from datetime import datetime, timedelta

import pytest

from backup.db import Job
from backup.runner import job_dir, list_snapshots, run_backup


def make_job(tmp_path, keep=7):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "a.txt").write_text("hello")
    return Job(
        name="docs", source=str(src), dest=str(dst),
        oncalendar="x", schedule_human="x", keep=keep,
        created_at="2026-06-28T00:00:00",
    )


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_snapshot_copies_files(tmp_path):
    job = make_job(tmp_path)
    res = run_backup(job, now=datetime(2026, 6, 28, 2, 0, 0))
    assert res.status == "ok"
    snaps = list_snapshots(job)
    assert len(snaps) == 1
    assert (snaps[0] / "a.txt").read_text() == "hello"
    assert (job_dir(job) / "latest").resolve() == snaps[0].resolve()


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_unchanged_files_are_hardlinked(tmp_path):
    job = make_job(tmp_path)
    run_backup(job, now=datetime(2026, 6, 28, 2, 0, 0))
    run_backup(job, now=datetime(2026, 6, 28, 3, 0, 0))
    snaps = list_snapshots(job)
    assert len(snaps) == 2
    ino0 = os.stat(snaps[0] / "a.txt").st_ino
    ino1 = os.stat(snaps[1] / "a.txt").st_ino
    assert ino0 == ino1  # hard-linked, no extra space


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_retention_prunes_oldest(tmp_path):
    job = make_job(tmp_path, keep=2)
    base = datetime(2026, 6, 28, 0, 0, 0)
    for i in range(4):
        run_backup(job, now=base + timedelta(hours=i))
    snaps = list_snapshots(job)
    assert len(snaps) == 2  # only newest 2 kept


def test_missing_dest_fails(tmp_path):
    job = make_job(tmp_path)
    shutil.rmtree(job.dest)  # destination base gone
    res = run_backup(job, now=datetime(2026, 6, 28, 2, 0, 0))
    assert res.status == "failed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src python -m pytest tests/test_runner.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backup.runner'`

- [ ] **Step 3: Write `src/backup/runner.py`**

```python
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from . import db, paths

TIMESTAMP_FMT = "%Y-%m-%d_%H-%M-%S"
_RSYNC_OK = {0, 24}  # 24 = some source files vanished during transfer


@dataclass
class RunResult:
    status: str
    message: str
    snapshot: Optional[str]


def job_dir(job: db.Job) -> Path:
    return Path(job.dest) / job.name


def _snapshots_dir(job: db.Job) -> Path:
    return job_dir(job) / "snapshots"


def list_snapshots(job: db.Job) -> List[Path]:
    snaps = _snapshots_dir(job)
    if not snaps.is_dir():
        return []
    dirs = [
        p for p in snaps.iterdir()
        if p.is_dir() and not p.name.endswith(".partial")
    ]
    return sorted(dirs, key=lambda p: p.name)


def _log(job: db.Job, message: str) -> None:
    try:
        paths.log_dir().mkdir(parents=True, exist_ok=True)
        logfile = paths.log_dir() / ("%s.log" % job.name)
        with logfile.open("a") as fh:
            fh.write("%s %s\n" % (datetime.now().isoformat(timespec="seconds"), message))
    except OSError:
        pass  # logging must never crash a backup run


def _prune(job: db.Job) -> None:
    snaps = list_snapshots(job)
    excess = len(snaps) - job.keep
    for old in snaps[:max(0, excess)]:
        shutil.rmtree(old, ignore_errors=True)


def run_backup(
    job: db.Job, conn=None, now: Optional[datetime] = None
) -> RunResult:
    now = now or datetime.now()
    source = Path(job.source)
    dest_base = Path(job.dest)

    if not source.is_dir():
        return _finish(job, conn, now, "failed",
                       "source missing: %s" % source, None)
    if not dest_base.is_dir():
        return _finish(job, conn, now, "failed",
                       "destination missing: %s" % dest_base, None)

    snaps_dir = _snapshots_dir(job)
    snaps_dir.mkdir(parents=True, exist_ok=True)

    stamp = now.strftime(TIMESTAMP_FMT)
    final = snaps_dir / stamp
    partial = snaps_dir / ("%s.partial" % stamp)
    if partial.exists():
        shutil.rmtree(partial, ignore_errors=True)

    previous = list_snapshots(job)
    cmd = ["rsync", "-a", "--delete"]
    if previous:
        cmd.append("--link-dest=%s" % previous[-1])
    cmd.append("%s/" % source)
    cmd.append("%s/" % partial)

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode not in _RSYNC_OK:
        shutil.rmtree(partial, ignore_errors=True)
        msg = "rsync failed (code %d): %s" % (
            result.returncode, result.stderr.strip())
        return _finish(job, conn, now, "failed", msg, None)

    partial.replace(final)
    _update_latest(job, final)
    _prune(job)

    msg = "snapshot %s (%d kept)" % (stamp, len(list_snapshots(job)))
    return _finish(job, conn, now, "ok", msg, str(final))


def _update_latest(job: db.Job, snapshot: Path) -> None:
    link = job_dir(job) / "latest"
    if link.is_symlink() or link.exists():
        try:
            link.unlink()
        except OSError:
            return
    try:
        link.symlink_to(Path("snapshots") / snapshot.name)
    except OSError:
        pass


def _finish(job, conn, now, status, message, snapshot) -> RunResult:
    _log(job, "%s: %s" % (status, message))
    if conn is not None:
        db.record_run(conn, job.name, status, message,
                      now.isoformat(timespec="seconds"))
    return RunResult(status=status, message=message, snapshot=snapshot)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src python -m pytest tests/test_runner.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/backup/runner.py tests/test_runner.py
git commit -m "feat: rsync hard-link snapshot runner with retention"
```

---

## Task 6: `cli.py` — argparse dispatch + validation

**Files:**
- Create: `src/backup/cli.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: all prior modules.
- Produces (`backup.cli`):
  - `main(argv: Optional[List[str]] = None) -> int` — entry point; returns process exit code
  - `slugify(text: str) -> str` — lowercase, non-alnum → `-`, trimmed; used for default job name
- Validation rules in `add`: source resolved + must be a dir; dest resolved; dest must not be inside source; name unique; source not already registered; schedule parsed + `validate_oncalendar` must pass.

- [ ] **Step 1: Write the failing test** — `tests/test_cli.py`

```python
from __future__ import annotations

import backup.cli as cli
import backup.units as units


def _silence_systemd(monkeypatch):
    monkeypatch.setattr(units, "_systemctl", lambda *a: _CP())
    monkeypatch.setattr(units, "is_active", lambda name: True)
    monkeypatch.setattr(units, "next_run", lambda name: None)


class _CP:
    returncode = 0
    stdout = ""


def test_slugify():
    assert cli.slugify("My Docs!") == "my-docs"


def test_add_creates_job_and_units(xdg, tmp_path, monkeypatch, capsys):
    _silence_systemd(monkeypatch)
    src = tmp_path / "proj"
    dst = tmp_path / "bak"
    src.mkdir()
    dst.mkdir()
    rc = cli.main(["add", "--source", str(src), "--dest", str(dst),
                   "--schedule", "daily@02:00"])
    assert rc == 0
    svc, timer = units.unit_paths("proj")
    assert svc.exists() and timer.exists()


def test_add_rejects_dest_inside_source(xdg, tmp_path, monkeypatch, capsys):
    _silence_systemd(monkeypatch)
    src = tmp_path / "proj"
    src.mkdir()
    inside = src / "backups"
    rc = cli.main(["add", "--source", str(src), "--dest", str(inside),
                   "--schedule", "hourly"])
    assert rc != 0
    assert "inside" in capsys.readouterr().err.lower()


def test_add_rejects_duplicate_source(xdg, tmp_path, monkeypatch):
    _silence_systemd(monkeypatch)
    src = tmp_path / "proj"
    dst = tmp_path / "bak"
    src.mkdir()
    dst.mkdir()
    assert cli.main(["add", "--source", str(src), "--dest", str(dst),
                     "--schedule", "hourly"]) == 0
    assert cli.main(["add", "--source", str(src), "--dest", str(dst),
                     "--schedule", "hourly", "--name", "other"]) != 0


def test_list_and_remove(xdg, tmp_path, monkeypatch, capsys):
    _silence_systemd(monkeypatch)
    src = tmp_path / "proj"
    dst = tmp_path / "bak"
    src.mkdir()
    dst.mkdir()
    cli.main(["add", "--source", str(src), "--dest", str(dst), "--schedule", "hourly"])
    assert cli.main(["list"]) == 0
    assert "proj" in capsys.readouterr().out
    assert cli.main(["remove", "proj"]) == 0
    assert cli.main(["remove", "proj"]) != 0  # already gone
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src python -m pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backup.cli'`

- [ ] **Step 3: Write `src/backup/cli.py`**

```python
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from . import db, paths, runner, schedule, units


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return slug or "job"


def _err(msg: str) -> int:
    print("error: %s" % msg, file=sys.stderr)
    return 1


def _resolve(p: str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(p)))


def _is_inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def cmd_add(args) -> int:
    source = _resolve(args.source or os.getcwd())
    if not source.is_dir():
        return _err("source is not a directory: %s" % source)
    dest = _resolve(args.dest)
    if _is_inside(dest, source) or dest == source:
        return _err("destination %s is inside source %s (would recurse)"
                    % (dest, source))

    name = args.name or slugify(source.name)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        return _err("invalid name %r (use lowercase letters, digits, hyphens)" % name)

    try:
        sched = schedule.parse_schedule(args.schedule)
    except ValueError as exc:
        return _err(str(exc))
    if not schedule.validate_oncalendar(sched.oncalendar):
        return _err("systemd rejected schedule: %s" % sched.oncalendar)

    conn = db.connect()
    if db.get_job(conn, name) is not None:
        return _err("a job named %r already exists" % name)
    if db.get_job_by_source(conn, str(source)) is not None:
        existing = db.get_job_by_source(conn, str(source))
        return _err("source already registered as job %r" % existing.name)

    dest.mkdir(parents=True, exist_ok=True)
    job = db.Job(
        name=name, source=str(source), dest=str(dest),
        oncalendar=sched.oncalendar, schedule_human=sched.human,
        keep=args.keep, created_at=datetime.now().isoformat(timespec="seconds"),
    )
    db.add_job(conn, job)
    units.install_units(name, sched.oncalendar, paths.backup_executable(), str(source))
    print("added job %r: %s -> %s (%s, keep %d)"
          % (name, source, dest, sched.human, args.keep))
    return 0


def _require_job(conn, name: str):
    job = db.get_job(conn, name)
    if job is None:
        print("error: no job named %r" % name, file=sys.stderr)
    return job


def cmd_list(args) -> int:
    conn = db.connect()
    jobs = db.list_jobs(conn)
    if not jobs:
        print("no backup jobs registered. add one with: backup add --dest <path>")
        return 0
    header = "%-14s %-8s %-18s %-20s %s" % (
        "NAME", "STATE", "SCHEDULE", "LAST RUN", "SOURCE -> DEST")
    print(header)
    for job in jobs:
        state = "active" if units.is_active(job.name) else "paused"
        last = "%s %s" % (job.last_run_at or "-", job.last_status or "")
        print("%-14s %-8s %-18s %-20s %s -> %s" % (
            job.name, state, job.schedule_human, last.strip(),
            job.source, job.dest))
    return 0


def cmd_status(args) -> int:
    conn = db.connect()
    job = _require_job(conn, args.name)
    if job is None:
        return 1
    state = "active" if units.is_active(job.name) else "paused"
    print("job:       %s" % job.name)
    print("source:    %s" % job.source)
    print("dest:      %s" % job.dest)
    print("schedule:  %s (%s)" % (job.schedule_human, job.oncalendar))
    print("retention: keep %d snapshots" % job.keep)
    print("state:     %s" % state)
    print("last run:  %s [%s] %s" % (
        job.last_run_at or "-", job.last_status or "-", job.last_message or ""))
    nxt = units.next_run(job.name)
    if nxt:
        print("next:      %s" % nxt)
    logfile = paths.log_dir() / ("%s.log" % job.name)
    if logfile.exists():
        tail = logfile.read_text().splitlines()[-5:]
        print("recent log:")
        for line in tail:
            print("  %s" % line)
    return 0


def cmd_remove(args) -> int:
    conn = db.connect()
    job = _require_job(conn, args.name)
    if job is None:
        return 1
    units.remove_units(job.name)
    db.remove_job(conn, job.name)
    if args.purge:
        import shutil
        shutil.rmtree(runner.job_dir(job), ignore_errors=True)
        print("removed job %r and purged snapshots" % job.name)
    else:
        print("removed job %r (snapshots kept at %s)"
              % (job.name, runner.job_dir(job)))
    return 0


def cmd_pause(args) -> int:
    conn = db.connect()
    job = _require_job(conn, args.name)
    if job is None:
        return 1
    units.pause_units(job.name)
    print("paused %r" % job.name)
    return 0


def cmd_resume(args) -> int:
    conn = db.connect()
    job = _require_job(conn, args.name)
    if job is None:
        return 1
    units.resume_units(job.name)
    print("resumed %r" % job.name)
    return 0


def cmd_run(args) -> int:
    conn = db.connect()
    job = _require_job(conn, args.name)
    if job is None:
        return 1
    result = runner.run_backup(job, conn=conn)
    print("%s: %s" % (result.status, result.message))
    return 0 if result.status == "ok" else 1


def cmd_internal_run(args) -> int:
    conn = db.connect()
    job = db.get_job(conn, args.name)
    if job is None:
        return _err("no job named %r" % args.name)
    result = runner.run_backup(job, conn=conn)
    return 0 if result.status == "ok" else 1


def cmd_edit(args) -> int:
    conn = db.connect()
    job = _require_job(conn, args.name)
    if job is None:
        return 1
    updates = {}
    oncalendar = job.oncalendar
    if args.schedule:
        try:
            sched = schedule.parse_schedule(args.schedule)
        except ValueError as exc:
            return _err(str(exc))
        if not schedule.validate_oncalendar(sched.oncalendar):
            return _err("systemd rejected schedule: %s" % sched.oncalendar)
        updates["oncalendar"] = sched.oncalendar
        updates["schedule_human"] = sched.human
        oncalendar = sched.oncalendar
    if args.keep is not None:
        updates["keep"] = args.keep
    if args.dest:
        updates["dest"] = str(_resolve(args.dest))
    if args.rename:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", args.rename):
            return _err("invalid name %r" % args.rename)
        if db.get_job(conn, args.rename) is not None:
            return _err("a job named %r already exists" % args.rename)

    new_name = args.rename or job.name
    if args.rename:
        units.remove_units(job.name)
        updates["name"] = args.rename
    db.update_job(conn, job.name, **updates)
    updated = db.get_job(conn, new_name)
    units.install_units(updated.name, oncalendar,
                        paths.backup_executable(), updated.source)
    print("updated %r" % new_name)
    return 0


def cmd_snapshots(args) -> int:
    conn = db.connect()
    job = _require_job(conn, args.name)
    if job is None:
        return 1
    snaps = runner.list_snapshots(job)
    if not snaps:
        print("no snapshots yet for %r" % job.name)
        return 0
    for snap in reversed(snaps):
        size = _dir_size(snap)
        print("%-22s %s" % (snap.name, _human(size)))
    return 0


def cmd_restore(args) -> int:
    conn = db.connect()
    job = _require_job(conn, args.name)
    if job is None:
        return 1
    snaps = runner.list_snapshots(job)
    if not snaps:
        return _err("no snapshots to restore for %r" % job.name)
    if args.snapshot:
        chosen = next((s for s in snaps if s.name == args.snapshot), None)
        if chosen is None:
            return _err("snapshot %r not found" % args.snapshot)
    else:
        chosen = snaps[-1]
    target = _resolve(args.to) if args.to else (
        Path(job.source).parent / ("restore-%s" % chosen.name))
    import subprocess
    subprocess.run(["rsync", "-a", "%s/" % chosen, "%s/" % target], check=False)
    print("restored %s -> %s" % (chosen.name, target))
    return 0


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            fp = Path(root) / f
            try:
                total += fp.stat().st_size
            except OSError:
                pass
    return total


def _human(n: int) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024:
            return "%d%s" % (n, unit)
        n //= 1024
    return "%dP" % n


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="backup", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("add", help="register the current dir as a backup job")
    a.add_argument("--source", help="directory to back up (default: cwd)")
    a.add_argument("--dest", required=True, help="local destination directory")
    a.add_argument("--schedule", default="daily@02:00",
                   help="hourly | daily@HH:MM | weekly@dow:HH:MM | every:Nh | every:Nm")
    a.add_argument("--keep", type=int, default=7, help="snapshots to retain")
    a.add_argument("--name", help="job name (default: source basename)")
    a.set_defaults(func=cmd_add)

    sub.add_parser("list", help="list jobs").set_defaults(func=cmd_list)

    for cmd, fn, help_ in [
        ("status", cmd_status, "show job detail"),
        ("pause", cmd_pause, "pause a job's timer"),
        ("resume", cmd_resume, "resume a job's timer"),
        ("run", cmd_run, "run a backup now"),
        ("snapshots", cmd_snapshots, "list snapshots for a job"),
    ]:
        sp = sub.add_parser(cmd, help=help_)
        sp.add_argument("name")
        sp.set_defaults(func=fn)

    r = sub.add_parser("remove", help="delete a job")
    r.add_argument("name")
    r.add_argument("--purge", action="store_true", help="also delete snapshots")
    r.set_defaults(func=cmd_remove)

    e = sub.add_parser("edit", help="modify a job")
    e.add_argument("name")
    e.add_argument("--schedule")
    e.add_argument("--keep", type=int)
    e.add_argument("--dest")
    e.add_argument("--rename")
    e.set_defaults(func=cmd_edit)

    rs = sub.add_parser("restore", help="restore a snapshot")
    rs.add_argument("name")
    rs.add_argument("--snapshot", help="timestamp dir name (default: newest)")
    rs.add_argument("--to", help="destination dir (default: restore-<ts> by source)")
    rs.set_defaults(func=cmd_restore)

    ir = sub.add_parser("_run", help=argparse.SUPPRESS)
    ir.add_argument("name")
    ir.set_defaults(func=cmd_internal_run)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src python -m pytest tests/test_cli.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Run the full suite**

Run: `PYTHONPATH=src python -m pytest -v`
Expected: PASS (all tests across all files)

- [ ] **Step 6: Commit**

```bash
git add src/backup/cli.py tests/test_cli.py
git commit -m "feat: argparse CLI dispatch with validation"
```

---

## Task 7: Install scripts + docs

**Files:**
- Create: `install.sh`, `uninstall.sh`, `README.md`, `LICENSE`

**Interfaces:**
- Consumes: the installed package.
- Produces: a working `backup` command on PATH and a documented repo.

- [ ] **Step 1: Write `install.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARE_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/backup"
BIN_DIR="$HOME/.local/bin"
BIN="$BIN_DIR/backup"

echo "Installing backup CLI..."
mkdir -p "$SHARE_DIR" "$BIN_DIR"
rm -rf "$SHARE_DIR/backup"
cp -r "$REPO_DIR/src/backup" "$SHARE_DIR/backup"

cat > "$BIN" <<EOF
#!/usr/bin/env bash
exec python3 -c 'import sys; sys.path.insert(0, "$SHARE_DIR"); from backup.cli import main; sys.exit(main())' "\$@"
EOF
chmod +x "$BIN"

# Ensure ~/.local/bin is on PATH
if ! echo ":$PATH:" | grep -q ":$BIN_DIR:"; then
  RC="$HOME/.bashrc"
  [ -n "${ZSH_VERSION:-}" ] && RC="$HOME/.zshrc"
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$RC"
  echo "Added ~/.local/bin to PATH in $RC (open a new shell or 'source $RC')."
fi

# Create config/state dirs
"$BIN" list >/dev/null 2>&1 || true

# Enable linger so user timers run when logged out
if command -v loginctl >/dev/null 2>&1; then
  if loginctl enable-linger "$USER" 2>/dev/null; then
    echo "Enabled linger for $USER (timers run when logged out)."
  else
    echo "Note: could not enable linger; timers run only while you are logged in."
  fi
fi

echo "Done. Try:  backup add --dest /path/to/backups --schedule daily@02:00"
```

- [ ] **Step 2: Write `uninstall.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail

SHARE_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/backup"
BIN="$HOME/.local/bin/backup"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
PURGE="${1:-}"

if [ "$PURGE" = "--purge" ]; then
  echo "Stopping and removing all backup timers..."
  for timer in "$UNIT_DIR"/backup-*.timer; do
    [ -e "$timer" ] || continue
    unit="$(basename "$timer")"
    systemctl --user disable --now "$unit" 2>/dev/null || true
  done
  rm -f "$UNIT_DIR"/backup-*.timer "$UNIT_DIR"/backup-*.service
  systemctl --user daemon-reload 2>/dev/null || true
  rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/backup" \
         "${XDG_STATE_HOME:-$HOME/.local/state}/backup"
  echo "Purged jobs, timers, config, and state (snapshots on destinations kept)."
fi

rm -f "$BIN"
rm -rf "$SHARE_DIR"
echo "Removed backup CLI."
```

- [ ] **Step 3: Make scripts executable and test install end-to-end**

```bash
chmod +x install.sh uninstall.sh
bash install.sh
~/.local/bin/backup --help
```
Expected: help text listing `add`, `list`, `remove`, `pause`, `resume`, `run`, `status`, `edit`, `snapshots`, `restore`.

- [ ] **Step 4: Real end-to-end smoke test**

```bash
mkdir -p /tmp/bk-src /tmp/bk-dst && echo hi > /tmp/bk-src/f.txt
~/.local/bin/backup add --source /tmp/bk-src --dest /tmp/bk-dst --schedule daily@02:00 --name smoke
~/.local/bin/backup run smoke
~/.local/bin/backup snapshots smoke
~/.local/bin/backup list
~/.local/bin/backup remove smoke --purge
```
Expected: `run` prints `ok: snapshot ...`; `snapshots` lists one timestamp; `list` shows `smoke`; remove succeeds.

- [ ] **Step 5: Write `README.md`** (install, usage, how snapshots/restore work)

```markdown
# backup

Per-directory backups for Linux: register a folder, pick a schedule, and a
systemd **user timer** takes periodic **rsync hard-link snapshots** into a local
destination — keeping the most recent N and pruning the rest. Unchanged files
are hard-linked between snapshots, so each snapshot is a full browsable copy
that costs almost no extra disk space.

## Requirements

- Linux with `systemd` (user instance), `rsync`, and `python3` (3.9+).
- No root required; installs under `~/.local`.

## Install

```bash
git clone <repo-url> backup-tool
cd backup-tool
bash install.sh
```

The installer puts `backup` on your `PATH` (`~/.local/bin`) and enables linger so
timers run even when you are logged out.

## Usage

```bash
# In the folder you want to back up:
cd ~/important-project
backup add --dest /mnt/backups --schedule daily@02:00

backup list                 # see all jobs, state, last/next run
backup status important-project
backup run important-project    # snapshot now
backup pause important-project  # stop future runs
backup resume important-project
backup snapshots important-project
backup edit important-project --keep 14 --schedule weekly@sun:03:00
backup restore important-project --to /tmp/recovered
backup remove important-project           # keep snapshots
backup remove important-project --purge   # also delete snapshots
```

### Schedules

`hourly` · `daily@HH:MM` · `weekly@dow:HH:MM` (dow = mon..sun) · `every:Nh` ·
`every:Nm`. For full control pass a raw systemd expression via the timer (see
`man systemd.time`).

### Where things live

- Job registry: `~/.config/backup/jobs.db`
- Logs: `~/.local/state/backup/logs/<name>.log`
- Timers: `~/.config/systemd/user/backup-<name>.timer`
- Snapshots: `<dest>/<name>/snapshots/<timestamp>/`, with a `latest` symlink.

## Uninstall

```bash
bash uninstall.sh           # remove the CLI
bash uninstall.sh --purge   # also remove all jobs, timers, config, and state
```

Snapshots already written to your destinations are never deleted automatically.
```

- [ ] **Step 6: Write `LICENSE`** (MIT)

```text
MIT License

Copyright (c) 2026 William

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

- [ ] **Step 7: Commit**

```bash
git add install.sh uninstall.sh README.md LICENSE
git commit -m "feat: install/uninstall scripts and documentation"
```

---

## Self-Review Notes

- **Spec coverage:** add/list/remove/pause/resume/run/status/edit/snapshots/restore → Task 6; systemd timers → Task 4; rsync hard-link snapshots + keep-N prune → Task 5; SQLite registry → Task 2; schedule presets + raw OnCalendar validation → Task 3; per-user install + linger → Task 7; XDG paths → Task 1. All spec sections map to a task.
- **Validation rules** (source is dir, dest-not-inside-source, duplicate name/source, schedule validity) are covered by tests in Task 6.
- **Type consistency:** `Job` fields, `Schedule(oncalendar, human)`, `RunResult(status, message, snapshot)`, and the `units` function names (`install_units`, `remove_units`, `pause_units`, `resume_units`, `is_active`, `next_run`, `unit_paths`, `_systemctl`) are used identically across Tasks 4–6.
- **No placeholders:** every code step contains complete, runnable code.
```
