# Multiple Default Destinations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `backup config` can hold a list of default destinations, and `backup add` without `--dest` creates one job per default destination.

**Architecture:** The `config` table's `default_dest` value becomes a JSON array of absolute paths (legacy plain-path values are read as a one-element list). `db.py` gains `get_default_dests`/`set_default_dests`; `cmd_config` gains `--add-default-dest`/`--remove-default-dest` (mutually exclusive with `--default-dest`, which now replaces the whole list); `cmd_add` fans out over the list when `--dest` is omitted.

**Tech Stack:** Python 3 stdlib (argparse, sqlite3, json), pytest.

**Spec:** `docs/superpowers/specs/2026-08-09-multi-default-dest-design.md`

## Global Constraints

- No new dependencies; stdlib only.
- Job names must match `[a-z0-9][a-z0-9-]*` (existing rule).
- With exactly 1 default destination (or explicit `--dest`), `backup add` behavior is byte-for-byte unchanged: unsuffixed job name, existing error messages.
- Fan-out (≥2 defaults) is atomic w.r.t. inside-source validation: if any default is inside the source, create nothing.
- Run tests with `python -m pytest tests/ -q` from the repo root.

---

### Task 1: db helpers for the destination list

**Files:**
- Modify: `src/backup/db.py` (add `import json` at top; new functions after `set_config`, ~line 227)
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: existing `get_config(conn, key)` / `set_config(conn, key, value)` in `src/backup/db.py`.
- Produces: `get_default_dests(conn: sqlite3.Connection) -> List[str]` and `set_default_dests(conn: sqlite3.Connection, dests: List[str]) -> None`, both in `backup.db`. Tasks 2 and 3 call these.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_db.py` (and extend the existing `from backup.db import (...)` block with `get_default_dests, set_default_dests`):

```python
def test_get_default_dests_missing_returns_empty(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    assert get_default_dests(conn) == []


def test_set_and_get_default_dests_round_trip(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    set_default_dests(conn, ["/mnt/nas", "/mnt/usb"])
    assert get_default_dests(conn) == ["/mnt/nas", "/mnt/usb"]


def test_get_default_dests_reads_legacy_plain_path(tmp_path):
    # A db written before the JSON-list format stores a bare path string.
    conn = connect(tmp_path / "jobs.db")
    set_config(conn, "default_dest", "/mnt/backups")
    assert get_default_dests(conn) == ["/mnt/backups"]


def test_set_default_dests_overwrites(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    set_default_dests(conn, ["/a"])
    set_default_dests(conn, ["/b", "/c"])
    assert get_default_dests(conn) == ["/b", "/c"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_db.py -q`
Expected: 4 failures — ImportError/`cannot import name 'get_default_dests'`.

- [ ] **Step 3: Implement the helpers**

In `src/backup/db.py`: add `import json` next to `import sqlite3`; after `set_config` add:

```python
def get_default_dests(conn: sqlite3.Connection) -> List[str]:
    """Default destinations, in insertion order.

    The value is a JSON array; a bare path written by an older version is
    read as a one-element list (rewritten as JSON on the next set).
    """
    raw = get_config(conn, "default_dest")
    if raw is None:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return [raw]
    if isinstance(parsed, list):
        return [str(p) for p in parsed]
    return [raw]


def set_default_dests(conn: sqlite3.Connection, dests: List[str]) -> None:
    set_config(conn, "default_dest", json.dumps(dests))
```

(Name the parameter `dests`, not `paths` — the module already imports `paths`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_db.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/backup/db.py tests/test_db.py
git commit -m "feat(db): default_dest holds a JSON list; legacy value read as one-element list"
```

---

### Task 2: `backup config` add/remove/replace/show

**Files:**
- Modify: `src/backup/cli.py` — `cmd_config` (~line 118) and the `config` subparser in `build_parser()` (~line 447)
- Test: `tests/test_cli.py` (config tests live around line 199)

**Interfaces:**
- Consumes: `db.get_default_dests(conn)`, `db.set_default_dests(conn, dests)` from Task 1; existing `_resolve`, `_err`.
- Produces: CLI flags `--add-default-dest`, `--remove-default-dest`; `--default-dest` now replaces the whole list. Display format `default-dest: a, b` / `default-dest: (not set)`. Task 3 relies only on the db helpers, not on this task's code.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cli.py` after `test_config_show_when_unset`:

```python
def test_config_add_appends_default_dests(xdg, tmp_path, capsys):
    import backup.db as db
    a, b = tmp_path / "nas", tmp_path / "usb"
    assert cli.main(["config", "--add-default-dest", str(a)]) == 0
    assert cli.main(["config", "--add-default-dest", str(b)]) == 0
    assert a.is_dir() and b.is_dir()
    conn = db.connect()
    assert db.get_default_dests(conn) == [str(a), str(b)]
    assert "%s, %s" % (a, b) in capsys.readouterr().out


def test_config_add_is_idempotent(xdg, tmp_path, capsys):
    import backup.db as db
    a = tmp_path / "nas"
    assert cli.main(["config", "--add-default-dest", str(a)]) == 0
    assert cli.main(["config", "--add-default-dest", str(a)]) == 0
    conn = db.connect()
    assert db.get_default_dests(conn) == [str(a)]
    assert "already" in capsys.readouterr().err.lower()


def test_config_remove_default_dest(xdg, tmp_path, capsys):
    import backup.db as db
    a, b = tmp_path / "nas", tmp_path / "usb"
    cli.main(["config", "--add-default-dest", str(a)])
    cli.main(["config", "--add-default-dest", str(b)])
    assert cli.main(["config", "--remove-default-dest", str(a)]) == 0
    conn = db.connect()
    assert db.get_default_dests(conn) == [str(b)]
    assert a.is_dir()  # the directory itself is not deleted


def test_config_remove_unknown_dest_errors(xdg, tmp_path, capsys):
    rc = cli.main(["config", "--remove-default-dest", str(tmp_path / "nope")])
    assert rc != 0
    assert "not a default destination" in capsys.readouterr().err


def test_config_default_dest_replaces_list(xdg, tmp_path, capsys):
    import backup.db as db
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    cli.main(["config", "--add-default-dest", str(a)])
    cli.main(["config", "--add-default-dest", str(b)])
    assert cli.main(["config", "--default-dest", str(c)]) == 0
    conn = db.connect()
    assert db.get_default_dests(conn) == [str(c)]


def test_config_add_and_remove_conflict(xdg, tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["config", "--add-default-dest", str(tmp_path / "a"),
                  "--remove-default-dest", str(tmp_path / "b")])
```

Also update the last two lines of the existing `test_config_sets_and_shows_default_dest` (~line 208) — the stored value is now JSON:

```python
    conn = db.connect()
    assert db.get_default_dests(conn) == [str(dst)]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cli.py -q -k config`
Expected: new tests fail with argparse `SystemExit: 2` (unrecognized `--add-default-dest`); `test_config_sets_and_shows_default_dest` fails on the new assertion (value still stored as a bare path).

- [ ] **Step 3: Implement**

Replace `cmd_config` in `src/backup/cli.py` with:

```python
def cmd_config(args) -> int:
    conn = db.connect()
    if args.default_dest is not None:
        dest = _resolve(args.default_dest)
        dest.mkdir(parents=True, exist_ok=True)
        db.set_default_dests(conn, [str(dest)])
    elif args.add_default_dest is not None:
        dest = _resolve(args.add_default_dest)
        dest.mkdir(parents=True, exist_ok=True)
        dests = db.get_default_dests(conn)
        if str(dest) in dests:
            print("note: %s is already a default destination" % dest,
                  file=sys.stderr)
        else:
            dests.append(str(dest))
            db.set_default_dests(conn, dests)
    elif args.remove_default_dest is not None:
        dest = _resolve(args.remove_default_dest)
        dests = db.get_default_dests(conn)
        if str(dest) not in dests:
            return _err("%s is not a default destination" % dest)
        dests.remove(str(dest))
        db.set_default_dests(conn, dests)
    current = db.get_default_dests(conn)
    print("default-dest: %s" % (", ".join(current) if current else "(not set)"))
    return 0
```

Replace the `config` subparser block in `build_parser()` with:

```python
    c = sub.add_parser("config", help="show or set configuration")
    g = c.add_mutually_exclusive_group()
    g.add_argument("--default-dest", dest="default_dest",
                   help="replace the default destination list with this single path")
    g.add_argument("--add-default-dest", dest="add_default_dest",
                   help="append a destination used by 'add' when --dest is omitted")
    g.add_argument("--remove-default-dest", dest="remove_default_dest",
                   help="remove a path from the default destination list")
    c.set_defaults(func=cmd_config)
```

Also required in this task (the key's stored format changed, and `cmd_add`
still reads it with `get_config` — the pre-existing add tests would break):
in `cmd_add` (~line 64), replace

```python
    dest_arg = args.dest or db.get_config(conn, "default_dest")
```

with

```python
    defaults = db.get_default_dests(conn)
    dest_arg = args.dest or (defaults[0] if len(defaults) == 1 else None)
```

This stopgap keeps single-default behavior working and is replaced wholesale
by Task 3.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/ -q`
Expected: full suite green.

- [ ] **Step 5: Commit**

```bash
git add src/backup/cli.py tests/test_cli.py
git commit -m "feat(cli): config --add/--remove-default-dest manage a destination list"
```

---

### Task 3: `backup add` fans out over the default list

**Files:**
- Modify: `src/backup/cli.py` — `cmd_add` (~line 58) and `_confirm_duplicate_source` (~line 39)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `db.get_default_dests` (Task 1); existing `db.get_job`, `db.list_jobs_by_source`, `db.add_job`, `db.remove_job`, `units.install_units`, `_confirm_duplicate_source`, `slugify`, `_resolve`, `_is_inside`, `schedule.parse_schedule`, `schedule.validate_oncalendar`.
- Produces: fan-out behavior of `backup add`; job naming `<base>-<slugify(dest.name)>` with `-2`, `-3`… dedup. No new public functions besides module-private `_fanout_names`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cli.py`:

```python
def _add_defaults(*dests):
    for d in dests:
        assert cli.main(["config", "--add-default-dest", str(d)]) == 0


def test_add_fans_out_to_all_default_dests(xdg, tmp_path, monkeypatch, capsys):
    import backup.db as db
    _silence_systemd(monkeypatch)
    src = tmp_path / "proj"
    src.mkdir()
    nas, usb = tmp_path / "nas", tmp_path / "usb"
    _add_defaults(nas, usb)
    assert cli.main(["add", "--source", str(src), "--schedule", "hourly"]) == 0
    conn = db.connect()
    jobs = db.list_jobs_by_source(conn, str(src))
    assert {(j.name, j.dest) for j in jobs} == {
        ("proj-nas", str(nas)), ("proj-usb", str(usb))}
    for name in ("proj-nas", "proj-usb"):
        svc, timer = units.unit_paths(name)
        assert svc.exists() and timer.exists()
    out = capsys.readouterr().out
    assert out.count("added job") == 2


def test_add_fanout_suffixes_colliding_dest_names(xdg, tmp_path, monkeypatch):
    import backup.db as db
    _silence_systemd(monkeypatch)
    src = tmp_path / "proj"
    src.mkdir()
    a, b = tmp_path / "disk1" / "bk", tmp_path / "disk2" / "bk"
    _add_defaults(a, b)
    assert cli.main(["add", "--source", str(src), "--schedule", "hourly"]) == 0
    conn = db.connect()
    names = sorted(j.name for j in db.list_jobs_by_source(conn, str(src)))
    assert names == ["proj-bk", "proj-bk-2"]


def test_add_fanout_skips_existing_pair(xdg, tmp_path, monkeypatch, capsys):
    import backup.db as db
    _silence_systemd(monkeypatch)
    src = tmp_path / "proj"
    src.mkdir()
    nas, usb = tmp_path / "nas", tmp_path / "usb"
    _add_defaults(nas)
    assert cli.main(["add", "--source", str(src), "--schedule", "hourly"]) == 0
    _add_defaults(usb)
    capsys.readouterr()
    assert cli.main(["add", "--source", str(src), "--schedule", "hourly",
                     "--yes"]) == 0
    err = capsys.readouterr().err
    assert "skipping" in err and str(nas) in err
    conn = db.connect()
    dests = {j.dest for j in db.list_jobs_by_source(conn, str(src))}
    assert dests == {str(nas), str(usb)}


def test_add_fanout_all_existing_errors(xdg, tmp_path, monkeypatch, capsys):
    _silence_systemd(monkeypatch)
    src = tmp_path / "proj"
    src.mkdir()
    nas, usb = tmp_path / "nas", tmp_path / "usb"
    _add_defaults(nas, usb)
    assert cli.main(["add", "--source", str(src), "--schedule", "hourly"]) == 0
    capsys.readouterr()
    rc = cli.main(["add", "--source", str(src), "--schedule", "hourly", "--yes"])
    assert rc != 0
    assert "all default destinations" in capsys.readouterr().err


def test_add_fanout_aborts_if_any_dest_inside_source(xdg, tmp_path, monkeypatch, capsys):
    import backup.db as db
    _silence_systemd(monkeypatch)
    src = tmp_path / "proj"
    src.mkdir()
    _add_defaults(tmp_path / "ok", src / "inside")
    rc = cli.main(["add", "--source", str(src), "--schedule", "hourly"])
    assert rc != 0
    assert "inside" in capsys.readouterr().err.lower()
    conn = db.connect()
    assert db.list_jobs(conn) == []  # atomic: nothing created


def test_add_fanout_continues_past_install_failure(xdg, tmp_path, monkeypatch, capsys):
    import backup.db as db
    calls = []

    def flaky(name, oncalendar, exe, source):
        calls.append(name)
        if len(calls) == 1:
            raise RuntimeError("boom")

    monkeypatch.setattr(units, "install_units", flaky)
    monkeypatch.setattr(units, "is_active", lambda name: True)
    monkeypatch.setattr(units, "next_run", lambda name: None)
    src = tmp_path / "proj"
    src.mkdir()
    nas, usb = tmp_path / "nas", tmp_path / "usb"
    _add_defaults(nas, usb)
    rc = cli.main(["add", "--source", str(src), "--schedule", "hourly"])
    assert rc == 1
    conn = db.connect()
    jobs = db.list_jobs_by_source(conn, str(src))
    assert [(j.name, j.dest) for j in jobs] == [("proj-usb", str(usb))]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cli.py -q -k "fanout or fans_out"`
Expected: all new tests fail — with ≥2 defaults, the Task-2 stopgap makes `add` error "no destination".

- [ ] **Step 3: Implement**

In `src/backup/cli.py`, change `_confirm_duplicate_source`'s prompt line so `dest` may be a display string covering several paths (signature unchanged, one line reworded):

```python
    reply = input("Add another backup of this source to %s? [y/N] " % dest)
```
becomes
```python
    reply = input("Add backup(s) of this source to %s? [y/N] " % dest)
```

Add a helper above `cmd_add`:

```python
def _fanout_names(conn, base: str, dests: List[Path]) -> List[str]:
    """One unique job name per destination: <base>-<dest slug>, then -2, -3…"""
    names: List[str] = []
    taken = set()
    for dest in dests:
        stem = "%s-%s" % (base, slugify(dest.name))
        name, i = stem, 1
        while name in taken or db.get_job(conn, name) is not None:
            i += 1
            name = "%s-%d" % (stem, i)
        taken.add(name)
        names.append(name)
    return names
```

Replace `cmd_add` with:

```python
def cmd_add(args) -> int:
    source = _resolve(args.source or os.getcwd())
    if not source.is_dir():
        return _err("source is not a directory: %s" % source)

    conn = db.connect()
    if args.dest:
        dests = [_resolve(args.dest)]
    else:
        dests = [_resolve(d) for d in db.get_default_dests(conn)]
        if not dests:
            return _err("no destination: pass --dest or set one with "
                        "'backup config --default-dest <path>' (or several "
                        "with 'backup config --add-default-dest <path>')")
    for dest in dests:
        if _is_inside(dest, source) or dest == source:
            return _err("destination %s is inside source %s (would recurse)"
                        % (dest, source))
    fanout = len(dests) > 1

    base = args.name or slugify(source.name)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", base):
        return _err("invalid name %r (use lowercase letters, digits, hyphens)" % base)
    if args.keep < 1:
        return _err("--keep must be at least 1")
    if any(ord(c) < 32 for c in str(source)):
        return _err("source path contains control characters")

    try:
        sched = schedule.parse_schedule(args.schedule)
    except ValueError as exc:
        return _err(str(exc))
    if not schedule.validate_oncalendar(sched.oncalendar):
        return _err("systemd rejected schedule: %s" % sched.oncalendar)

    same_source = db.list_jobs_by_source(conn, str(source))
    pending: List[Path] = []
    for dest in dests:
        dup = next((j for j in same_source if j.dest == str(dest)), None)
        if dup is None:
            pending.append(dest)
        elif fanout:
            sys.stderr.write("note: skipping %s: already backed up as job %r\n"
                             % (dest, dup.name))
        else:
            return _err("source already backed up to %s as job %r"
                        % (dest, dup.name))
    if not pending:
        return _err("source already backed up to all default destinations")

    if fanout:
        names = _fanout_names(conn, base, pending)
    else:
        clash = db.get_job(conn, base)
        if clash is not None:
            hint = (" (pass --name to add another backup of the same source)"
                    if clash.source == str(source) else "")
            return _err("a job named %r already exists%s" % (base, hint))
        names = [base]

    if same_source:
        shown = ", ".join(str(d) for d in pending)
        if not _confirm_duplicate_source(source, shown, same_source, args.yes):
            return 1

    failures = 0
    for name, dest in zip(names, pending):
        dest.mkdir(parents=True, exist_ok=True)
        job = db.Job(
            name=name, source=str(source), dest=str(dest),
            oncalendar=sched.oncalendar, schedule_human=sched.human,
            keep=args.keep, created_at=datetime.now().isoformat(timespec="seconds"),
            job_id=uuid.uuid4().hex,
        )
        db.add_job(conn, job)
        try:
            units.install_units(name, sched.oncalendar,
                                paths.backup_executable(), str(source))
        except RuntimeError as exc:
            db.remove_job(conn, name)
            sys.stderr.write("error: failed to install timer for %r: %s\n"
                             % (name, exc))
            failures += 1
            continue
        print("added job %r: %s -> %s (%s, keep %d)"
              % (name, source, dest, sched.human, args.keep))
    return 1 if failures else 0
```

Ordering notes (behavior kept from the old code): explicit `--dest` and single-default still produce the old messages; the inside-source check now runs for every destination before anything is created; `test_add_rolls_back_on_install_failure` still passes because a single-dest install failure removes the job and returns 1 (the message gains the job name — that test only checks rc and the DB).

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: all pass, including the pre-existing add/config/edit/run tests.

- [ ] **Step 5: Commit**

```bash
git add src/backup/cli.py tests/test_cli.py
git commit -m "feat(cli): backup add fans out over multiple default destinations"
```

---

### Task 4: README update

**Files:**
- Modify: `README.md` (usage section ~lines 27-29; multi-destination section ~lines 96-108)

**Interfaces:**
- Consumes: final CLI behavior from Tasks 2-3. Nothing downstream.

- [ ] **Step 1: Update the usage section**

Replace lines 28-29 (`# Set a default destination once...` block) with:

```bash
# Set a default destination once, so you don't repeat --dest:
backup config --default-dest /mnt/backups
# Or keep several — `backup add` then creates one job per destination:
backup config --add-default-dest /media/usb
backup config --remove-default-dest /media/usb
backup config               # show current settings
```

- [ ] **Step 2: Update the multi-destination section**

After the existing paragraph about `--name`/`--yes` (~line 108), add:

```markdown
With multiple **default** destinations configured (`--add-default-dest`),
`backup add` without `--dest` does this fan-out automatically: one job per
default destination, named `<base>-<destination-dir>` (e.g. `proj-nas`,
`proj-usb`), where `<base>` is `--name` or the folder name. Destinations the
source is already backed up to are skipped with a note.
```

- [ ] **Step 3: Verify and commit**

Run: `python -m pytest tests/ -q` (still green — docs only).

```bash
git add README.md
git commit -m "docs: multiple default destinations"
```

---

## Verification

1. `python -m pytest tests/ -q` — full suite green.
2. Manual smoke (optional, uses real systemd user units):
   `backup config --add-default-dest /tmp/bk1 && backup config --add-default-dest /tmp/bk2 && cd <some dir> && backup add --schedule hourly && backup list` → two jobs, then `backup remove` both.
3. Legacy compat: a `jobs.db` from before this change (bare-path `default_dest`) must behave as a one-element list — covered by `test_get_default_dests_reads_legacy_plain_path`.
