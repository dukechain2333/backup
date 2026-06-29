# Destination Integrity Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Before each backup, verify the destination is the same place and lineage as last time (identity marker + recorded last snapshot); on mismatch refuse the run and latch the job "blocked" until the user reconciles via `backup logs` + `backup run --force`.

**Architecture:** A new `integrity.py` module owns the destination marker (`<dest>/<name>/.backup-meta.json`) and the `verify(job)` check. `runner.run_backup` gains a `force` flag, calls `verify` (unless forced), writes the marker on success, and records `last_snapshot`/`blocked_reason` in the DB. Three new nullable `jobs` columns carry the per-job identity and blocked state, added through the existing `_ADDED_COLUMNS` migration. The CLI gains `run --force`, a `logs` command, and blocked surfacing.

**Tech Stack:** Python 3.9 (stdlib only — `json`, `uuid`, `dataclasses`, `pathlib`), pytest, real `rsync` in tests.

## Global Constraints

- Python 3.9 compatible — `from __future__ import annotations` at top of every module; no `match`, no PEP 604 `X | Y` runtime unions; `typing.Optional`/`typing.Tuple`/`typing.List`.
- Stdlib only in `src/backup/`.
- New `jobs` columns are **nullable** (SQLite cannot ADD a NOT NULL column without a default to a populated table) and registered in `db._ADDED_COLUMNS` for in-place upgrade of old DBs.
- Marker file path is exactly `<dest>/<name>/.backup-meta.json`; it must never be written inside `snapshots/` (rsync `--delete` territory).
- `RunResult.status` values: `"ok"`, `"failed"`, `"blocked"`. Exit code is non-zero for `failed` and `blocked`.
- `verify(job)` returns `Tuple[bool, Optional[str]]` — `(True, None)` to proceed, `(False, reason)` to block.

---

## File Structure

```
src/backup/integrity.py     # NEW: marker_path/read_marker/write_marker/verify
src/backup/db.py            # MODIFY: 3 new Job fields + schema + _COLUMNS + _ADDED_COLUMNS + INSERT
src/backup/runner.py        # MODIFY: run_backup(force=...), blocked latch, verify, marker on success
src/backup/cli.py           # MODIFY: run --force, logs cmd, blocked in list/status, add assigns job_id
tests/test_integrity.py     # NEW
tests/test_db.py            # MODIFY: new columns present/defaults
tests/test_runner.py        # MODIFY: blocked latch, verify-block, force re-baseline, marker write
tests/test_cli.py           # MODIFY: run --force, logs, blocked surfacing, add job_id
README.md                   # MODIFY: document integrity + logs + --force
```

---

## Task 1: DB columns for identity and blocked state

**Files:**
- Modify: `src/backup/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: existing `connect`, `_ADDED_COLUMNS`, `Job`, `add_job`, `update_job`.
- Produces: `Job` gains `job_id: Optional[str] = None`, `last_snapshot: Optional[str] = None`, `blocked_reason: Optional[str] = None`. New DBs get these columns from `_SCHEMA`; old DBs via `_ADDED_COLUMNS`.

- [ ] **Step 1: Write the failing test** — append to `tests/test_db.py`

```python
def test_new_jobs_have_identity_columns(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, make_job())
    job = get_job(conn, "docs")
    assert job.job_id is None
    assert job.last_snapshot is None
    assert job.blocked_reason is None


def test_identity_columns_are_updatable(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, make_job())
    update_job(conn, "docs", job_id="abc123", last_snapshot="2026-06-29_01-00-00",
               blocked_reason="dest moved")
    job = get_job(conn, "docs")
    assert job.job_id == "abc123"
    assert job.last_snapshot == "2026-06-29_01-00-00"
    assert job.blocked_reason == "dest moved"


def test_old_db_upgrades_with_identity_columns(tmp_path):
    import backup.db as dbmod
    path = tmp_path / "jobs.db"
    # Build a pre-feature jobs table (no identity columns)
    import sqlite3
    raw = sqlite3.connect(str(path))
    raw.executescript("""
        CREATE TABLE jobs (name TEXT PRIMARY KEY, source TEXT NOT NULL UNIQUE,
        dest TEXT NOT NULL, oncalendar TEXT NOT NULL, schedule_human TEXT NOT NULL,
        keep INTEGER NOT NULL, created_at TEXT NOT NULL, last_run_at TEXT,
        last_status TEXT, last_message TEXT);
        INSERT INTO jobs VALUES ('legacy','/s','/d','hourly','every hour',7,
        '2026-06-01T00:00:00',NULL,NULL,NULL);
    """)
    raw.commit()
    raw.close()
    conn = dbmod.connect(path)  # new code opens it
    for col in ("job_id", "last_snapshot", "blocked_reason"):
        assert dbmod._column_exists(conn, "jobs", col)
    assert get_job(conn, "legacy").source == "/s"  # data survived
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /home/william/backup-tool && PYTHONPATH=src python -m pytest tests/test_db.py -k "identity_columns or old_db_upgrades_with_identity" -v`
Expected: FAIL — `TypeError`/`AttributeError` (Job has no `job_id`) or missing columns.

- [ ] **Step 3: Implement** — in `src/backup/db.py`:

(a) Add the three columns to `_COLUMNS`:
```python
_COLUMNS = (
    "name", "source", "dest", "oncalendar", "schedule_human",
    "keep", "created_at", "last_run_at", "last_status", "last_message",
    "job_id", "last_snapshot", "blocked_reason",
)
```

(b) Add them to the `jobs` table in `_SCHEMA` (after `last_message   TEXT`):
```python
    last_message   TEXT,
    job_id         TEXT,
    last_snapshot  TEXT,
    blocked_reason TEXT
```

(c) Add fields to the `Job` dataclass (after `last_message`):
```python
    last_message: Optional[str] = None
    job_id: Optional[str] = None
    last_snapshot: Optional[str] = None
    blocked_reason: Optional[str] = None
```

(d) Extend `add_job`'s INSERT to include them:
```python
        conn.execute(
            "INSERT INTO jobs (name, source, dest, oncalendar, schedule_human, "
            "keep, created_at, last_run_at, last_status, last_message, "
            "job_id, last_snapshot, blocked_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job.name, job.source, job.dest, job.oncalendar,
                job.schedule_human, job.keep, job.created_at,
                job.last_run_at, job.last_status, job.last_message,
                job.job_id, job.last_snapshot, job.blocked_reason,
            ),
        )
```

(e) Register them for old-DB upgrade in `_ADDED_COLUMNS`:
```python
_ADDED_COLUMNS = [
    ("jobs", "job_id", "TEXT"),
    ("jobs", "last_snapshot", "TEXT"),
    ("jobs", "blocked_reason", "TEXT"),
]
```

- [ ] **Step 4: Run to verify it passes**

Run: `PYTHONPATH=src python -m pytest tests/test_db.py -v`
Expected: PASS (all existing + 3 new).

- [ ] **Step 5: Commit**

```bash
git add src/backup/db.py tests/test_db.py
git commit -m "feat: add job_id/last_snapshot/blocked_reason columns"
```

---

## Task 2: `integrity.py` — marker + verify

**Files:**
- Create: `src/backup/integrity.py`, `tests/test_integrity.py`

**Interfaces:**
- Consumes (`backup.db`): `Job`.
- Produces (`backup.integrity`):
  - `marker_path(job: db.Job) -> Path` — `<dest>/<name>/.backup-meta.json`
  - `read_marker(job: db.Job) -> Optional[dict]` — parsed dict, or None if absent/corrupt
  - `write_marker(job: db.Job, last_snapshot: Optional[str]) -> None`
  - `verify(job: db.Job) -> Tuple[bool, Optional[str]]`

- [ ] **Step 1: Write the failing test** — `tests/test_integrity.py`

```python
from __future__ import annotations

from backup import integrity
from backup.db import Job


def make_job(tmp_path, name="proj", job_id="id-1", last_snapshot=None):
    dest = tmp_path / "dst"
    dest.mkdir(exist_ok=True)
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    return Job(
        name=name, source=str(src), dest=str(dest), oncalendar="hourly",
        schedule_human="every hour", keep=7, created_at="2026-06-29T00:00:00",
        job_id=job_id, last_snapshot=last_snapshot,
    )


def _make_snapshot(job, stamp):
    d = tmp = __import__("pathlib").Path(job.dest) / job.name / "snapshots" / stamp
    d.mkdir(parents=True, exist_ok=True)


def test_marker_roundtrip(tmp_path):
    job = make_job(tmp_path)
    assert integrity.read_marker(job) is None
    integrity.write_marker(job, "2026-06-29_01-00-00")
    data = integrity.read_marker(job)
    assert data["job_id"] == "id-1"
    assert data["source"] == job.source
    assert data["last_snapshot"] == "2026-06-29_01-00-00"


def test_corrupt_marker_reads_as_none(tmp_path):
    job = make_job(tmp_path)
    p = integrity.marker_path(job)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ not json")
    assert integrity.read_marker(job) is None


def test_verify_first_run_ok(tmp_path):
    job = make_job(tmp_path, last_snapshot=None)
    assert integrity.verify(job) == (True, None)


def test_verify_first_run_foreign_marker_blocks(tmp_path):
    job = make_job(tmp_path, job_id="id-1", last_snapshot=None)
    other = make_job(tmp_path, job_id="id-OTHER")
    integrity.write_marker(other, None)  # someone else's marker already there
    ok, reason = integrity.verify(job)
    assert ok is False
    assert "id-OTHER" in reason


def test_verify_match_ok(tmp_path):
    job = make_job(tmp_path, last_snapshot="2026-06-29_01-00-00")
    _make_snapshot(job, "2026-06-29_01-00-00")
    integrity.write_marker(job, "2026-06-29_01-00-00")
    assert integrity.verify(job) == (True, None)


def test_verify_marker_missing_blocks(tmp_path):
    job = make_job(tmp_path, last_snapshot="2026-06-29_01-00-00")
    _make_snapshot(job, "2026-06-29_01-00-00")
    ok, reason = integrity.verify(job)
    assert ok is False
    assert "marker missing" in reason


def test_verify_id_mismatch_blocks(tmp_path):
    job = make_job(tmp_path, job_id="id-1", last_snapshot="2026-06-29_01-00-00")
    _make_snapshot(job, "2026-06-29_01-00-00")
    integrity.write_marker(job, "2026-06-29_01-00-00")
    job.job_id = "id-CHANGED"
    ok, reason = integrity.verify(job)
    assert ok is False
    assert "different job" in reason


def test_verify_source_changed_blocks(tmp_path):
    job = make_job(tmp_path, last_snapshot="2026-06-29_01-00-00")
    _make_snapshot(job, "2026-06-29_01-00-00")
    integrity.write_marker(job, "2026-06-29_01-00-00")
    job.source = "/somewhere/else"
    ok, reason = integrity.verify(job)
    assert ok is False
    assert "source" in reason


def test_verify_missing_snapshot_blocks(tmp_path):
    job = make_job(tmp_path, last_snapshot="2026-06-29_01-00-00")
    integrity.write_marker(job, "2026-06-29_01-00-00")  # marker says so, but dir absent
    ok, reason = integrity.verify(job)
    assert ok is False
    assert "missing from destination" in reason


def test_verify_marker_out_of_sync_blocks(tmp_path):
    job = make_job(tmp_path, last_snapshot="2026-06-29_02-00-00")
    _make_snapshot(job, "2026-06-29_02-00-00")
    integrity.write_marker(job, "2026-06-29_01-00-00")  # marker lags DB
    ok, reason = integrity.verify(job)
    assert ok is False
    assert "out of sync" in reason
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=src python -m pytest tests/test_integrity.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backup.integrity'`.

- [ ] **Step 3: Implement** — `src/backup/integrity.py`

```python
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from . import db

MARKER_NAME = ".backup-meta.json"


def marker_path(job: db.Job) -> Path:
    return Path(job.dest) / job.name / MARKER_NAME


def read_marker(job: db.Job) -> Optional[dict]:
    try:
        with marker_path(job).open() as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_marker(job: db.Job, last_snapshot: Optional[str]) -> None:
    path = marker_path(job)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "job_id": job.job_id,
        "name": job.name,
        "source": job.source,
        "last_snapshot": last_snapshot,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    tmp = path.with_name(MARKER_NAME + ".tmp")
    with tmp.open("w") as fh:
        json.dump(data, fh, indent=2)
    tmp.replace(path)


def verify(job: db.Job) -> Tuple[bool, Optional[str]]:
    marker = read_marker(job)
    has_baseline = job.last_snapshot is not None

    if not has_baseline:
        if marker is not None and marker.get("job_id") != job.job_id:
            return False, "destination already belongs to job %s" % marker.get("job_id")
        return True, None

    if marker is None:
        return False, "destination marker missing (dest moved, unmounted, or wiped?)"
    if marker.get("job_id") != job.job_id:
        return False, "destination belongs to a different job (id mismatch)"
    if marker.get("source") != job.source:
        return False, "source path changed since last backup"
    snap = Path(job.dest) / job.name / "snapshots" / job.last_snapshot
    if not snap.is_dir():
        return False, ("recorded last snapshot %s missing from destination "
                       "(content changed)" % job.last_snapshot)
    if marker.get("last_snapshot") != job.last_snapshot:
        return False, "destination marker out of sync with records"
    return True, None
```

- [ ] **Step 4: Run to verify it passes**

Run: `PYTHONPATH=src python -m pytest tests/test_integrity.py -v`
Expected: PASS (11 passed).

- [ ] **Step 5: Commit**

```bash
git add src/backup/integrity.py tests/test_integrity.py
git commit -m "feat: destination marker and integrity verify"
```

---

## Task 3: Runner integration (force, blocked latch, marker on success)

**Files:**
- Modify: `src/backup/runner.py`
- Test: `tests/test_runner.py`

**Interfaces:**
- Consumes: `integrity.verify`, `integrity.write_marker`; `db.update_job`.
- Produces: `run_backup(job, conn=None, now=None, force=False) -> RunResult`. New `RunResult.status` value `"blocked"`. On success: marker written, `last_snapshot` recorded, `blocked_reason` cleared. On mismatch: `blocked_reason` set, no rsync. Blocked latch refuses non-forced runs.

- [ ] **Step 1: Write the failing test** — append to `tests/test_runner.py`

```python
import json as _json

from backup import integrity
from backup.db import connect, add_job, get_job


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_success_writes_marker_and_records_last_snapshot(tmp_path):
    job = make_job(tmp_path)
    job.job_id = "id-1"
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, job)
    res = run_backup(job, conn=conn, now=datetime(2026, 6, 28, 2, 0, 0))
    assert res.status == "ok"
    marker = integrity.read_marker(job)
    assert marker["job_id"] == "id-1"
    assert marker["last_snapshot"] == "2026-06-28_02-00-00"
    assert get_job(conn, job.name).last_snapshot == "2026-06-28_02-00-00"


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_mismatch_blocks_and_writes_no_snapshot(tmp_path):
    job = make_job(tmp_path)
    job.job_id = "id-1"
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, job)
    run_backup(job, conn=conn, now=datetime(2026, 6, 28, 2, 0, 0))  # baseline
    # Corrupt the destination: delete the marker so verify fails
    integrity.marker_path(job).unlink()
    reloaded = get_job(conn, job.name)
    res = run_backup(reloaded, conn=conn, now=datetime(2026, 6, 28, 3, 0, 0))
    assert res.status == "blocked"
    assert get_job(conn, job.name).blocked_reason is not None
    assert len(list_snapshots(job)) == 1  # no new snapshot created


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_blocked_latch_refuses_until_force(tmp_path):
    job = make_job(tmp_path)
    job.job_id = "id-1"
    job.blocked_reason = "previously blocked"
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, job)
    res = run_backup(job, conn=conn, now=datetime(2026, 6, 28, 2, 0, 0))
    assert res.status == "blocked"
    assert len(list_snapshots(job)) == 0  # nothing ran


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_force_rebaselines_and_clears_blocked(tmp_path):
    job = make_job(tmp_path)
    job.job_id = "id-1"
    job.blocked_reason = "previously blocked"
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, job)
    res = run_backup(job, conn=conn, now=datetime(2026, 6, 28, 2, 0, 0), force=True)
    assert res.status == "ok"
    assert get_job(conn, job.name).blocked_reason is None
    assert integrity.read_marker(job) is not None
```

(Note: the existing `make_job` helper in `tests/test_runner.py` builds a `Job` without `job_id`; these tests set `job.job_id` explicitly before use.)

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=src python -m pytest tests/test_runner.py -k "marker or mismatch_blocks or blocked_latch or force_rebaselines" -v`
Expected: FAIL — `run_backup() got an unexpected keyword argument 'force'` (and no marker written).

- [ ] **Step 3: Implement** — edit `src/backup/runner.py`

(a) Add import at top (after `from . import db, paths`):
```python
from . import db, integrity, paths
```

(b) Replace the `run_backup` function with:
```python
def run_backup(
    job: db.Job, conn=None, now: Optional[datetime] = None, force: bool = False
) -> RunResult:
    now = now or datetime.now()
    source = Path(job.source)
    dest_base = Path(job.dest)

    if job.blocked_reason and not force:
        return _finish(
            job, conn, now, "blocked",
            "still blocked: %s; run 'backup run %s --force' to override"
            % (job.blocked_reason, job.name), None)

    # Ensure the job has a stable identity (legacy jobs created before this feature)
    if job.job_id is None:
        import uuid
        job.job_id = uuid.uuid4().hex
        if conn is not None:
            db.update_job(conn, job.name, job_id=job.job_id)

    if not source.is_dir():
        return _finish(job, conn, now, "failed",
                       "source missing: %s" % source, None)
    if not dest_base.is_dir():
        return _finish(job, conn, now, "failed",
                       "destination missing: %s" % dest_base, None)

    if not force:
        ok, reason = integrity.verify(job)
        if not ok:
            if conn is not None:
                db.update_job(conn, job.name, blocked_reason=reason)
            return _finish(job, conn, now, "blocked",
                           "verification failed: %s" % reason, None)

    snaps_dir = _snapshots_dir(job)
    snaps_dir.mkdir(parents=True, exist_ok=True)

    stamp = now.strftime(TIMESTAMP_FMT)
    final = snaps_dir / stamp
    partial = snaps_dir / ("%s.partial" % stamp)
    if partial.is_dir() or partial.is_symlink():
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

    integrity.write_marker(job, stamp)
    if conn is not None:
        db.update_job(conn, job.name, last_snapshot=stamp, blocked_reason=None)

    suffix = " (forced, re-baselined)" if force else ""
    msg = "snapshot %s (%d kept)%s" % (stamp, len(list_snapshots(job)), suffix)
    return _finish(job, conn, now, "ok", msg, str(final))
```

- [ ] **Step 4: Run to verify it passes**

Run: `PYTHONPATH=src python -m pytest tests/test_runner.py -v`
Expected: PASS (existing + 4 new).

- [ ] **Step 5: Commit**

```bash
git add src/backup/runner.py tests/test_runner.py
git commit -m "feat: verify destination before backup; force re-baselines"
```

---

## Task 4: CLI — run --force, logs, blocked surfacing, add assigns job_id

**Files:**
- Modify: `src/backup/cli.py`, `README.md`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `runner.run_backup(..., force=...)`, `db.Job(job_id=...)`, `paths.log_dir()`.
- Produces: `run --force` flag; `cmd_logs`; `list`/`status` show `blocked`; `add` sets `job_id`.

- [ ] **Step 1: Write the failing test** — append to `tests/test_cli.py`

```python
def test_add_assigns_job_id(xdg, tmp_path, monkeypatch):
    import backup.db as db
    _silence_systemd(monkeypatch)
    src = tmp_path / "proj"
    dst = tmp_path / "bak"
    src.mkdir()
    dst.mkdir()
    cli.main(["add", "--source", str(src), "--dest", str(dst), "--schedule", "hourly"])
    conn = db.connect()
    assert db.get_job(conn, "proj").job_id is not None


def test_list_shows_blocked(xdg, tmp_path, monkeypatch, capsys):
    import backup.db as db
    _silence_systemd(monkeypatch)
    src = tmp_path / "proj"
    dst = tmp_path / "bak"
    src.mkdir()
    dst.mkdir()
    cli.main(["add", "--source", str(src), "--dest", str(dst), "--schedule", "hourly"])
    conn = db.connect()
    db.update_job(conn, "proj", blocked_reason="dest moved")
    capsys.readouterr()
    cli.main(["list"])
    assert "blocked" in capsys.readouterr().out.lower()


def test_logs_prints_log(xdg, tmp_path, monkeypatch, capsys):
    from backup import paths
    _silence_systemd(monkeypatch)
    src = tmp_path / "proj"
    dst = tmp_path / "bak"
    src.mkdir()
    dst.mkdir()
    cli.main(["add", "--source", str(src), "--dest", str(dst), "--schedule", "hourly"])
    logfile = paths.log_dir() / "proj.log"
    logfile.parent.mkdir(parents=True, exist_ok=True)
    logfile.write_text("2026-06-29T00:00:00 ok: snapshot\n2026-06-29T01:00:00 blocked: x\n")
    capsys.readouterr()
    assert cli.main(["logs", "proj"]) == 0
    out = capsys.readouterr().out
    assert "blocked: x" in out


def test_logs_missing_log(xdg, tmp_path, monkeypatch, capsys):
    _silence_systemd(monkeypatch)
    src = tmp_path / "proj"
    dst = tmp_path / "bak"
    src.mkdir()
    dst.mkdir()
    cli.main(["add", "--source", str(src), "--dest", str(dst), "--schedule", "hourly"])
    capsys.readouterr()
    assert cli.main(["logs", "proj"]) == 0
    assert "no log" in capsys.readouterr().out.lower()


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_run_force_clears_blocked_and_runs(xdg, tmp_path, monkeypatch):
    import backup.db as db
    _silence_systemd(monkeypatch)
    src = tmp_path / "proj"
    dst = tmp_path / "bak"
    src.mkdir()
    dst.mkdir()
    (src / "f.txt").write_text("hi")
    cli.main(["add", "--source", str(src), "--dest", str(dst), "--schedule", "hourly"])
    conn = db.connect()
    db.update_job(conn, "proj", blocked_reason="dest moved")
    assert cli.main(["run", "proj", "--force"]) == 0
    assert db.get_job(conn, "proj").blocked_reason is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=src python -m pytest tests/test_cli.py -k "job_id or blocked or logs or force_clears" -v`
Expected: FAIL — `add` doesn't set `job_id`; `logs` is an invalid choice; `--force` unrecognized.

- [ ] **Step 3: Implement** — edit `src/backup/cli.py`

(a) Add `import uuid` to the top stdlib imports (next to `import sys`).

(b) In `cmd_add`, set `job_id` when building the `Job` (add the kwarg to the existing `db.Job(...)` call):
```python
    job = db.Job(
        name=name, source=str(source), dest=str(dest),
        oncalendar=sched.oncalendar, schedule_human=sched.human,
        keep=args.keep, created_at=datetime.now().isoformat(timespec="seconds"),
        job_id=uuid.uuid4().hex,
    )
```

(c) In `cmd_list`, replace the `state = ...` line with blocked precedence:
```python
        if job.blocked_reason:
            state = "blocked"
        else:
            state = "active" if units.is_active(job.name) else "paused"
```

(d) In `cmd_status`, replace the `state = ...` line the same way, and after the `state:` print add a blocked line:
```python
    if job.blocked_reason:
        state = "blocked"
    else:
        state = "active" if units.is_active(job.name) else "paused"
```
and immediately after `print("state:     %s" % state)` add:
```python
    if job.blocked_reason:
        print("blocked:   %s" % job.blocked_reason)
```

(e) Replace `cmd_run` to thread `force` through both paths:
```python
def cmd_run(args) -> int:
    conn = db.connect()
    if args.all and args.name:
        return _err("give a job name or --all, not both")
    if not args.all and not args.name:
        return _err("specify a job name or --all")

    if args.all:
        jobs = db.list_jobs(conn)
        if not jobs:
            print("no backup jobs registered.")
            return 0
        ok = 0
        failed = 0
        for job in jobs:
            result = runner.run_backup(job, conn=conn, force=args.force)
            print("%s: %s: %s" % (job.name, result.status, result.message))
            if result.status == "ok":
                ok += 1
            else:
                failed += 1
        print("%d ok, %d failed" % (ok, failed))
        return 0 if failed == 0 else 1

    job = _require_job(conn, args.name)
    if job is None:
        return 1
    result = runner.run_backup(job, conn=conn, force=args.force)
    print("%s: %s" % (result.status, result.message))
    return 0 if result.status == "ok" else 1
```

(f) Add a `cmd_logs` handler (place it next to `cmd_status`):
```python
def cmd_logs(args) -> int:
    conn = db.connect()
    job = _require_job(conn, args.name)
    if job is None:
        return 1
    logfile = paths.log_dir() / ("%s.log" % job.name)
    if not logfile.exists():
        print("no log yet for %r" % job.name)
        return 0
    lines = logfile.read_text().splitlines()
    for line in lines[-args.lines:]:
        print(line)
    return 0
```

(g) In `build_parser`, add `--force` to the `run` parser (the dedicated `rn` block) and register `logs`:
```python
    rn = sub.add_parser("run", help="run a backup now (one job, or --all)")
    rn.add_argument("name", nargs="?", help="job to run (omit with --all)")
    rn.add_argument("--all", action="store_true", help="run every job")
    rn.add_argument("--force", action="store_true",
                    help="skip integrity check, clear blocked, and re-baseline")
    rn.set_defaults(func=cmd_run)

    lg = sub.add_parser("logs", help="show a job's log")
    lg.add_argument("name")
    lg.add_argument("--lines", type=int, default=40, help="lines to show (default 40)")
    lg.set_defaults(func=cmd_logs)
```

- [ ] **Step 4: Run to verify it passes**

Run: `PYTHONPATH=src python -m pytest tests/test_cli.py -v`
Expected: PASS (existing + 5 new).

- [ ] **Step 5: Run the full suite**

Run: `PYTHONPATH=src python -m pytest -q`
Expected: PASS (all files).

- [ ] **Step 6: Update README**

In `README.md`, under the usage block add:
```markdown
backup logs important-project        # view this job's log (last 40 lines)
backup run important-project --force # override an integrity block & re-baseline
```
And add a short section after the schedules section:
```markdown
### Destination integrity

Each job writes a small marker (`<dest>/<name>/.backup-meta.json`) recording its
identity and last snapshot. Before every run, `backup` checks the destination
still matches — same job, same source, and the recorded last snapshot still
present. If it doesn't (wrong/unmounted drive, wiped or replaced snapshots), the
run is refused and the job is marked **blocked**; scheduled runs keep refusing
until you reconcile. Inspect with `backup logs <name>` and, once you're sure the
destination is correct, re-baseline with `backup run <name> --force`.
```

- [ ] **Step 7: Commit**

```bash
git add src/backup/cli.py tests/test_cli.py README.md
git commit -m "feat: run --force, logs command, blocked surfacing, add assigns job_id"
```

---

## Self-Review Notes

- **Spec coverage:** data model (Task 1); marker + verify branches (Task 2); blocked latch, mismatch→blocked, force re-baseline, marker on success (Task 3); `run --force`, `logs`, blocked in list/status, `add` job_id, README (Task 4). Migration of the 3 columns is exercised by `test_old_db_upgrades_with_identity_columns` (Task 1) atop the existing `_ADDED_COLUMNS` engine.
- **Type consistency:** `verify -> (bool, Optional[str])`, `RunResult.status in {ok, failed, blocked}`, `run_backup(job, conn, now, force)`, `Job` new fields `job_id/last_snapshot/blocked_reason` — used identically across tasks.
- **No placeholders:** every step carries complete code.
- **Edge cases:** corrupt marker → None → treated as missing (Task 2 test); legacy job with no `job_id` self-baselines (Task 3 `job.job_id is None` branch); blocked job leaves snapshots untouched (Task 3 tests).
```
