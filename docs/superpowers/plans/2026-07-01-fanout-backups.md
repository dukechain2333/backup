# Fan-out Backups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let one source directory be registered as multiple backup jobs (each to its own destination), blocking only exact `(source, dest)` duplicates, and remind + confirm when a user adds another destination for an already-backed-up source.

**Architecture:** Drop the column-level `UNIQUE` on `jobs.source` and enforce a composite `UNIQUE(source, dest)` index instead; migrate legacy databases in place via a one-time, idempotent table rebuild. Replace `get_job_by_source` (single) with `list_jobs_by_source` (many). The `add` command gains a reminder + y/N confirmation (or `--yes`) when the source already exists at a different destination.

**Tech Stack:** Python 3.9 (stdlib only — `sqlite3`, `argparse`, `sys`), pytest.

## Global Constraints

- Python 3.9 compatible — `from __future__ import annotations` at top of every module; no `match`, no PEP 604 `X | Y` runtime unions; `typing.List`/`Optional`.
- Stdlib only in `src/backup/`.
- Every job is identified by its `name` (primary key); nothing keys off `source` except the `add` duplicate check. Do not change how timers, snapshots, or integrity markers work.
- Uniqueness rule: same source + different dest = allowed; same source + same dest = rejected.
- `add` must never hang waiting for input: non-interactive (`not sys.stdin.isatty()`) without `--yes` refuses a duplicate source.

---

## File Structure

```
src/backup/db.py       # MODIFY: _SCHEMA (drop source UNIQUE); _migrate (legacy rebuild + composite index); replace get_job_by_source -> list_jobs_by_source
src/backup/cli.py      # MODIFY: cmd_add duplicate/fan-out logic + _confirm_duplicate_source helper; add --yes arg
tests/test_db.py       # MODIFY: import; uniqueness tests; list_jobs_by_source test; legacy-upgrade test
tests/test_cli.py      # MODIFY: duplicate-source test; fan-out/confirm/hint tests
README.md              # MODIFY: fan-out (one source -> many destinations) section
```

---

## Task 1: db.py — composite uniqueness, legacy migration, list_jobs_by_source

**Files:**
- Modify: `src/backup/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: existing `_SCHEMA`, `_COLUMNS`, `_migrate`, `_ensure_column`, `_row_to_job`, `add_job`, `list_jobs`, `get_job`.
- Produces: `list_jobs_by_source(conn, source: str) -> List[Job]` (all jobs whose `source` equals the argument, ordered by name); a composite unique index `jobs_source_dest` on `(source, dest)`; in-place upgrade of legacy `source`-UNIQUE databases. `get_job_by_source` is REMOVED.

- [ ] **Step 1: Write the failing tests** — edit `tests/test_db.py`

(a) In the top import block, replace `get_job_by_source` with `list_jobs_by_source`. The block becomes:
```python
from backup.db import (
    Job,
    add_job,
    connect,
    get_config,
    get_job,
    list_jobs,
    list_jobs_by_source,
    record_run,
    remove_job,
    set_config,
    update_job,
)
```

(b) Replace the existing `test_duplicate_source_rejected` (currently around line 48) with these two tests:
```python
def test_same_source_same_dest_rejected(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, make_job())                      # /a/docs -> /b
    with pytest.raises(ValueError):
        add_job(conn, make_job(name="other"))      # same source, same dest /b


def test_same_source_different_dest_allowed(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, make_job())                       # /a/docs -> /b
    add_job(conn, make_job(name="other", dest="/c"))  # same source, new dest
    assert {j.name for j in list_jobs(conn)} == {"docs", "other"}
```

(c) Replace the existing `test_get_by_source` (currently around line 55) with:
```python
def test_list_jobs_by_source(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, make_job())                               # docs  /a/docs -> /b
    add_job(conn, make_job(name="mirror", dest="/c"))       # mirror /a/docs -> /c
    add_job(conn, make_job(name="elsewhere", source="/a/other"))
    names = [j.name for j in list_jobs_by_source(conn, "/a/docs")]
    assert names == ["docs", "mirror"]      # both dests, ordered by name
    assert list_jobs_by_source(conn, "/nope") == []
```

(d) Append a legacy-upgrade test at the end of the file:
```python
def test_connect_upgrades_legacy_source_unique_db(tmp_path):
    import sqlite3
    path = tmp_path / "jobs.db"
    # Build a legacy DB whose jobs.source carries a column-level UNIQUE.
    legacy = sqlite3.connect(str(path))
    legacy.executescript(
        "CREATE TABLE jobs ("
        " name TEXT PRIMARY KEY, source TEXT NOT NULL UNIQUE, dest TEXT NOT NULL,"
        " oncalendar TEXT NOT NULL, schedule_human TEXT NOT NULL, keep INTEGER NOT NULL,"
        " created_at TEXT NOT NULL, last_run_at TEXT, last_status TEXT, last_message TEXT,"
        " job_id TEXT, last_snapshot TEXT, blocked_reason TEXT);"
        "CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
    )
    legacy.execute(
        "INSERT INTO jobs (name, source, dest, oncalendar, schedule_human, keep, created_at)"
        " VALUES ('docs','/a/docs','/b','*-*-* 02:00:00','daily at 02:00',7,'2026-06-28T00:00:00')"
    )
    legacy.commit()
    legacy.close()

    conn = connect(path)                                    # new code upgrades in place
    assert get_job(conn, "docs").source == "/a/docs"        # data preserved
    add_job(conn, make_job(name="mirror", dest="/c"))       # fan-out now allowed
    assert {j.name for j in list_jobs(conn)} == {"docs", "mirror"}
    with pytest.raises(ValueError):                         # same source+dest still rejected
        add_job(conn, make_job(name="dup"))                 # /a/docs -> /b again
    conn.close()

    conn2 = connect(path)                                   # idempotent: second upgrade is a no-op
    assert {j.name for j in list_jobs(conn2)} == {"docs", "mirror"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /home/william/backup-tool && PYTHONPATH=src python -m pytest tests/test_db.py -v`
Expected: FAIL — `ImportError: cannot import name 'list_jobs_by_source'` (collection error for the whole file).

- [ ] **Step 3: Implement** — edit `src/backup/db.py`

(a) In `_SCHEMA`, remove `UNIQUE` from the `source` line only. Change:
```python
    source         TEXT NOT NULL UNIQUE,
```
to:
```python
    source         TEXT NOT NULL,
```
(Leave the rest of `_SCHEMA` unchanged — do NOT add the index here; it is created in `_migrate`.)

(b) Add two migration helpers immediately after `_ensure_column` (before `_migrate`):
```python
def _legacy_source_unique(conn: sqlite3.Connection) -> bool:
    """True if jobs.source still carries the old single-column UNIQUE."""
    for idx in conn.execute("PRAGMA index_list(jobs)"):
        if idx["origin"] != "u" or not idx["unique"]:
            continue
        cols = [r["name"] for r in conn.execute(
            "PRAGMA index_info(%s)" % idx["name"])]
        if cols == ["source"]:
            return True
    return False


def _rebuild_jobs_without_source_unique(conn: sqlite3.Connection) -> None:
    """Recreate `jobs` without the column-level UNIQUE on source, preserving rows."""
    cols = ", ".join(_COLUMNS)
    conn.execute(
        "CREATE TABLE jobs_new ("
        " name TEXT PRIMARY KEY, source TEXT NOT NULL, dest TEXT NOT NULL,"
        " oncalendar TEXT NOT NULL, schedule_human TEXT NOT NULL, keep INTEGER NOT NULL,"
        " created_at TEXT NOT NULL, last_run_at TEXT, last_status TEXT, last_message TEXT,"
        " job_id TEXT, last_snapshot TEXT, blocked_reason TEXT)"
    )
    conn.execute("INSERT INTO jobs_new (%s) SELECT %s FROM jobs" % (cols, cols))
    conn.execute("DROP TABLE jobs")
    conn.execute("ALTER TABLE jobs_new RENAME TO jobs")
```

(c) Replace the body of `_migrate` with:
```python
def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, definition in _ADDED_COLUMNS:
        _ensure_column(conn, table, column, definition)
    conn.commit()
    if _legacy_source_unique(conn):
        conn.execute("BEGIN")
        try:
            _rebuild_jobs_without_source_unique(conn)
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS jobs_source_dest ON jobs(source, dest)"
    )
    conn.commit()
```

(d) Replace `get_job_by_source` with `list_jobs_by_source`:
```python
def list_jobs_by_source(conn: sqlite3.Connection, source: str) -> List[Job]:
    rows = conn.execute(
        "SELECT * FROM jobs WHERE source = ? ORDER BY name", (source,)
    ).fetchall()
    return [_row_to_job(r) for r in rows]
```

- [ ] **Step 4: Run to verify it passes**

Run: `PYTHONPATH=src python -m pytest tests/test_db.py -v`
Expected: PASS (existing tests + the 4 new/changed tests).

- [ ] **Step 5: Commit**

```bash
git add src/backup/db.py tests/test_db.py
git commit -m "feat(db): composite (source,dest) uniqueness + legacy migration; list_jobs_by_source"
```

---

## Task 2: cli.py — fan-out add flow (reminder + confirm + --yes) and README

**Files:**
- Modify: `src/backup/cli.py`, `README.md`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `db.list_jobs_by_source`, `db.get_job`, `db.list_jobs`, existing `_err`, `sys`.
- Produces: `_confirm_duplicate_source(source, dest, existing, assume_yes) -> bool`; updated `cmd_add`; a `--yes` flag on the `add` subparser.

- [ ] **Step 1: Write the failing tests** — edit `tests/test_cli.py`

(a) Replace the existing `test_add_rejects_duplicate_source` (currently around line 50) with:
```python
def test_add_rejects_same_source_same_dest(xdg, tmp_path, monkeypatch, capsys):
    _silence_systemd(monkeypatch)
    src = tmp_path / "proj"
    dst = tmp_path / "bak"
    src.mkdir()
    dst.mkdir()
    assert cli.main(["add", "--source", str(src), "--dest", str(dst),
                     "--schedule", "hourly"]) == 0
    assert cli.main(["add", "--source", str(src), "--dest", str(dst),
                     "--schedule", "hourly", "--name", "other"]) != 0
    assert "already backed up" in capsys.readouterr().err.lower()
```

(b) Append these fan-out / confirmation / hint tests:
```python
def test_add_fanout_same_source_new_dest_with_yes(xdg, tmp_path, monkeypatch, capsys):
    import backup.db as db
    _silence_systemd(monkeypatch)
    src = tmp_path / "proj"
    d1 = tmp_path / "bak1"
    d2 = tmp_path / "bak2"
    src.mkdir(); d1.mkdir(); d2.mkdir()
    assert cli.main(["add", "--source", str(src), "--dest", str(d1),
                     "--schedule", "hourly"]) == 0
    capsys.readouterr()
    rc = cli.main(["add", "--source", str(src), "--dest", str(d2),
                   "--schedule", "hourly", "--name", "mirror", "--yes"])
    assert rc == 0
    assert "already backed up" in capsys.readouterr().err.lower()   # reminder shown
    conn = db.connect()
    assert {j.name for j in db.list_jobs(conn)} == {"proj", "mirror"}


def test_add_duplicate_source_noninteractive_without_yes_refused(xdg, tmp_path, monkeypatch, capsys):
    _silence_systemd(monkeypatch)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    src = tmp_path / "proj"
    d1 = tmp_path / "bak1"
    d2 = tmp_path / "bak2"
    src.mkdir(); d1.mkdir(); d2.mkdir()
    cli.main(["add", "--source", str(src), "--dest", str(d1), "--schedule", "hourly"])
    capsys.readouterr()
    rc = cli.main(["add", "--source", str(src), "--dest", str(d2),
                   "--schedule", "hourly", "--name", "mirror"])
    assert rc != 0
    assert "--yes" in capsys.readouterr().err


def test_add_duplicate_source_tty_confirm_yes(xdg, tmp_path, monkeypatch, capsys):
    _silence_systemd(monkeypatch)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    src = tmp_path / "proj"
    d1 = tmp_path / "bak1"
    d2 = tmp_path / "bak2"
    src.mkdir(); d1.mkdir(); d2.mkdir()
    cli.main(["add", "--source", str(src), "--dest", str(d1), "--schedule", "hourly"])
    rc = cli.main(["add", "--source", str(src), "--dest", str(d2),
                   "--schedule", "hourly", "--name", "mirror"])
    assert rc == 0


def test_add_duplicate_source_tty_decline(xdg, tmp_path, monkeypatch, capsys):
    import backup.db as db
    _silence_systemd(monkeypatch)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    src = tmp_path / "proj"
    d1 = tmp_path / "bak1"
    d2 = tmp_path / "bak2"
    src.mkdir(); d1.mkdir(); d2.mkdir()
    cli.main(["add", "--source", str(src), "--dest", str(d1), "--schedule", "hourly"])
    rc = cli.main(["add", "--source", str(src), "--dest", str(d2),
                   "--schedule", "hourly", "--name", "mirror"])
    assert rc != 0
    conn = db.connect()
    assert {j.name for j in db.list_jobs(conn)} == {"proj"}   # second job not created


def test_add_same_source_no_name_hints_name_flag(xdg, tmp_path, monkeypatch, capsys):
    _silence_systemd(monkeypatch)
    src = tmp_path / "proj"
    d1 = tmp_path / "bak1"
    src.mkdir(); d1.mkdir()
    cli.main(["add", "--source", str(src), "--dest", str(d1), "--schedule", "hourly"])
    capsys.readouterr()
    rc = cli.main(["add", "--source", str(src), "--dest", str(tmp_path / "bak2"),
                   "--schedule", "hourly"])   # no --name -> name 'proj' collides
    assert rc != 0
    assert "--name" in capsys.readouterr().err
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=src python -m pytest tests/test_cli.py -k "add" -v`
Expected: FAIL — the fan-out add is currently rejected as "already registered"; `--yes` is an unrecognized argument (argparse SystemExit); messages don't match.

- [ ] **Step 3: Implement** — edit `src/backup/cli.py`

(a) Add the helper immediately above `cmd_add`:
```python
def _confirm_duplicate_source(source, dest, existing, assume_yes: bool) -> bool:
    """Warn that `source` is already backed up elsewhere; return True to proceed."""
    sys.stderr.write("note: %s is already backed up:\n" % source)
    for job in existing:
        sys.stderr.write("  - job %r -> %s\n" % (job.name, job.dest))
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        sys.stderr.write(
            "error: source already registered; re-run with --yes to add "
            "another destination\n")
        return False
    reply = input("Add another backup of this source to %s? [y/N] " % dest)
    if reply.strip().lower() in ("y", "yes"):
        return True
    print("aborted.")
    return False
```

(b) In `cmd_add`, replace the current duplicate-name / duplicate-source block:
```python
    if db.get_job(conn, name) is not None:
        return _err("a job named %r already exists" % name)
    existing = db.get_job_by_source(conn, str(source))
    if existing is not None:
        return _err("source already registered as job %r" % existing.name)
```
with:
```python
    clash = db.get_job(conn, name)
    if clash is not None:
        hint = (" (pass --name to add another backup of the same source)"
                if clash.source == str(source) else "")
        return _err("a job named %r already exists%s" % (name, hint))
    same_source = db.list_jobs_by_source(conn, str(source))
    dup = next((j for j in same_source if j.dest == str(dest)), None)
    if dup is not None:
        return _err("source already backed up to %s as job %r" % (dest, dup.name))
    if same_source and not _confirm_duplicate_source(source, dest, same_source, args.yes):
        return 1
```

(c) In `build_parser`, add the `--yes` flag to the `add` subparser (after the `--name` line, before `a.set_defaults`):
```python
    a.add_argument("--yes", action="store_true",
                   help="skip the confirmation when adding another destination "
                        "for an already-backed-up source")
```

- [ ] **Step 4: Run to verify it passes**

Run: `PYTHONPATH=src python -m pytest tests/test_cli.py -v`
Expected: PASS (existing + 6 new/changed add tests).

- [ ] **Step 5: Run the full suite**

Run: `PYTHONPATH=src python -m pytest -q`
Expected: PASS (all files).

- [ ] **Step 6: Update README** — edit `README.md`

Add a new section immediately after the "### Ignoring files (.backupignore)" section:
```markdown
### Backing up one folder to several places

The same folder can be registered as more than one job, each writing to its own
destination — for example a local disk and an external drive:

```
cd ~/important-project
backup add --dest /mnt/backups                       # first destination
backup add --dest /media/usb --name project-usb --yes  # second destination
```

Each destination needs its own job **name** (`--name`), since the default name
is derived from the folder. When you add a second destination for a folder that
is already backed up, `backup` reminds you of the existing job(s) and asks for
confirmation first; pass `--yes` to skip the prompt (required when running
non-interactively, e.g. in a script). Backing up the *same* folder to the
*same* destination twice is refused as a duplicate.
```

- [ ] **Step 7: Commit**

```bash
git add src/backup/cli.py tests/test_cli.py README.md
git commit -m "feat(cli): fan-out backups — reminder + confirm/--yes for duplicate source"
```

---

## Self-Review Notes

- **Spec coverage:** drop source UNIQUE + composite index (Task 1 §3a/3c); legacy migration idempotent + data-preserving (Task 1 helpers + `test_connect_upgrades_legacy_source_unique_db`); `get_job_by_source` → `list_jobs_by_source` (Task 1 §3d + test); add flow same-source/same-dest block, fan-out reminder + TTY confirm + non-TTY `--yes` refusal + name-collision hint (Task 2 §3b/§3a + 6 tests); README fan-out section (Task 2 §6). All spec sections map to a task.
- **Type consistency:** `list_jobs_by_source(conn, source: str) -> List[Job]` and `_confirm_duplicate_source(source, dest, existing, assume_yes) -> bool` used identically in defs and call sites; `--yes` → `args.yes`; the composite index name `jobs_source_dest` is identical in `_migrate` and the legacy test's implicit expectations.
- **No placeholders:** every step has complete code and exact commands.
- **Edge cases:** non-interactive add never blocks (isatty guard + `--yes`); duplicate `(source, dest)` caught pre-insert with the composite index as backstop; migration runs inside an explicit transaction (rollback on failure) and is idempotent (`CREATE UNIQUE INDEX IF NOT EXISTS`, legacy check skips when absent); declined confirmation creates no job.
```
