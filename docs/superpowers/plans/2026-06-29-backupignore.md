# .backupignore Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Honor nested gitignore-style `.backupignore` files during backups (via rsync's per-directory merge filter), and add `backup preview <name>` to dry-run the included file set.

**Architecture:** Add one rsync filter argument (`--filter=dir-merge,- .backupignore`) to the snapshot command in `runner.py`, defined once as `IGNORE_FILTER` and reused by a new `preview_backup(job)` that dry-runs into a throwaway temp dir and returns the would-be-backed-up relative paths. The CLI gains a `preview` subcommand.

**Tech Stack:** Python 3.9 (stdlib only — `subprocess`, `tempfile`, `shutil`, `pathlib`), `rsync` 3.x, pytest.

## Global Constraints

- Python 3.9 compatible — `from __future__ import annotations` at top of every module; no `match`, no PEP 604 `X | Y` runtime unions; `typing.List`.
- Stdlib only in `src/backup/`.
- The filter argument is exactly two argv tokens: `"--filter"` and `"dir-merge,- .backupignore"` (NOT one combined `--filter=...` string — pass as two list elements so rsync receives the rule verbatim).
- rsync success = exit code 0 or 24.
- Applied to every rsync invocation (normal run, forced run, preview); no stored state.

---

## File Structure

```
src/backup/runner.py     # MODIFY: IGNORE_FILTER constant; add to run_backup's cmd; new preview_backup()
src/backup/cli.py        # MODIFY: preview subcommand + cmd_preview
tests/test_runner.py     # MODIFY: ignore excludes (nested+top); preview lists included, writes nothing
tests/test_cli.py        # MODIFY: preview prints included/omits ignored; unknown job errors
README.md                # MODIFY: .backupignore section + preview usage
```

---

## Task 1: Runner — honor .backupignore + preview_backup

**Files:**
- Modify: `src/backup/runner.py`
- Test: `tests/test_runner.py`

**Interfaces:**
- Consumes: existing `run_backup`, `list_snapshots`, `_RSYNC_OK`, `db.Job`.
- Produces: module constant `IGNORE_FILTER = ["--filter", "dir-merge,- .backupignore"]`; `preview_backup(job: db.Job) -> List[str]` (sorted relative paths rsync would transfer, ignored entries excluded, root `.`/`./` entries removed; empty list if source missing or rsync errors). The same `IGNORE_FILTER` is added to `run_backup`'s rsync command.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_runner.py` (ensure `from pathlib import Path` is imported at the top of the file; add it if missing; add `preview_backup` to the existing `from backup.runner import ...` line)

```python
@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_backupignore_excludes_files_nested(tmp_path):
    job = make_job(tmp_path)
    job.job_id = "id-1"
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, job)
    src = Path(job.source)
    (src / "keep.txt").write_text("k")
    (src / "secret.log").write_text("s")
    (src / "sub").mkdir()
    (src / "sub" / "keep2.txt").write_text("k2")
    (src / "sub" / "tmp.cache").write_text("c")
    (src / ".backupignore").write_text("*.log\n")
    (src / "sub" / ".backupignore").write_text("*.cache\n")
    res = run_backup(job, conn=conn, now=datetime(2026, 6, 28, 2, 0, 0))
    assert res.status == "ok"
    snap = list_snapshots(job)[-1]
    assert (snap / "keep.txt").exists()
    assert (snap / "sub" / "keep2.txt").exists()
    assert not (snap / "secret.log").exists()       # top-level *.log ignored
    assert not (snap / "sub" / "tmp.cache").exists() # nested *.cache ignored


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_preview_lists_included_excludes_ignored_and_writes_nothing(tmp_path):
    job = make_job(tmp_path)
    src = Path(job.source)
    (src / "keep.txt").write_text("k")
    (src / "secret.log").write_text("s")
    (src / ".backupignore").write_text("*.log\n")
    files = preview_backup(job)
    assert "keep.txt" in files
    assert "secret.log" not in files
    assert list_snapshots(job) == []  # preview created no snapshot


def test_preview_missing_source_returns_empty(tmp_path):
    job = make_job(tmp_path)
    shutil.rmtree(job.source)
    assert preview_backup(job) == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /home/william/backup-tool && PYTHONPATH=src python -m pytest tests/test_runner.py -k "backupignore or preview" -v`
Expected: FAIL — `ImportError: cannot import name 'preview_backup'` (and the ignore test would fail since the filter isn't applied yet).

- [ ] **Step 3: Implement** — edit `src/backup/runner.py`

(a) Add `import tempfile` to the top imports (after `import subprocess`).

(b) Add the constant after `_RSYNC_OK`:
```python
IGNORE_FILTER = ["--filter", "dir-merge,- .backupignore"]
```

(c) In `run_backup`, add the filter to the rsync command — change:
```python
    previous = list_snapshots(job)
    cmd = ["rsync", "-a", "--delete"]
    if previous:
        cmd.append("--link-dest=%s" % previous[-1])
    cmd.append("%s/" % source)
    cmd.append("%s/" % partial)
```
to:
```python
    previous = list_snapshots(job)
    cmd = ["rsync", "-a", "--delete", *IGNORE_FILTER]
    if previous:
        cmd.append("--link-dest=%s" % previous[-1])
    cmd.append("%s/" % source)
    cmd.append("%s/" % partial)
```

(d) Add `preview_backup` (place it after `run_backup`, before `_update_latest`):
```python
def preview_backup(job: db.Job) -> List[str]:
    """Return the relative paths rsync would back up, with .backupignore applied.

    Dry-run against a throwaway empty directory; writes nothing to the real
    destination. Returns an empty list if the source is missing or rsync errors.
    """
    source = Path(job.source)
    if not source.is_dir():
        return []
    tmp = tempfile.mkdtemp(prefix="backup-preview-")
    try:
        cmd = ["rsync", "-rn", *IGNORE_FILTER, "--out-format=%n",
               "%s/" % source, "%s/" % tmp]
        result = subprocess.run(cmd, capture_output=True, text=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if result.returncode not in _RSYNC_OK:
        return []
    names = []
    for line in result.stdout.splitlines():
        name = line.strip()
        if name and name not in (".", "./"):
            names.append(name)
    return sorted(names)
```

- [ ] **Step 4: Run to verify it passes**

Run: `PYTHONPATH=src python -m pytest tests/test_runner.py -v`
Expected: PASS (existing + 3 new).

- [ ] **Step 5: Commit**

```bash
git add src/backup/runner.py tests/test_runner.py
git commit -m "feat: honor nested .backupignore and add preview_backup"
```

---

## Task 2: CLI `preview` command + README

**Files:**
- Modify: `src/backup/cli.py`, `README.md`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `runner.preview_backup(job)`; existing `_require_job`, `_err`, `db.connect`.
- Produces: `cmd_preview(args) -> int`; a `preview` subcommand.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_cli.py`

```python
@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_preview_prints_included_omits_ignored(xdg, tmp_path, monkeypatch, capsys):
    _silence_systemd(monkeypatch)
    src = tmp_path / "proj"
    dst = tmp_path / "bak"
    src.mkdir()
    dst.mkdir()
    (src / "keep.txt").write_text("k")
    (src / "secret.log").write_text("s")
    (src / ".backupignore").write_text("*.log\n")
    cli.main(["add", "--source", str(src), "--dest", str(dst), "--schedule", "hourly"])
    capsys.readouterr()
    assert cli.main(["preview", "proj"]) == 0
    out = capsys.readouterr().out
    assert "keep.txt" in out
    assert "secret.log" not in out


def test_preview_unknown_job_errors(xdg, tmp_path, monkeypatch, capsys):
    _silence_systemd(monkeypatch)
    rc = cli.main(["preview", "nope"])
    assert rc != 0
    assert "no job" in capsys.readouterr().err.lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=src python -m pytest tests/test_cli.py -k "preview" -v`
Expected: FAIL — `preview` is an invalid choice (argparse SystemExit).

- [ ] **Step 3: Implement** — edit `src/backup/cli.py`

(a) Add the handler (place it after `cmd_snapshots`):
```python
def cmd_preview(args) -> int:
    conn = db.connect()
    job = _require_job(conn, args.name)
    if job is None:
        return 1
    if not Path(job.source).is_dir():
        return _err("source is not a directory: %s" % job.source)
    files = runner.preview_backup(job)
    if not files:
        print("nothing to back up")
        return 0
    for path in files:
        print(path)
    return 0
```

(b) In `build_parser`, add `preview` to the shared single-`name` subcommand loop — change:
```python
    for cmd, fn, help_ in [
        ("status", cmd_status, "show job detail"),
        ("pause", cmd_pause, "pause a job's timer"),
        ("resume", cmd_resume, "resume a job's timer"),
        ("snapshots", cmd_snapshots, "list snapshots for a job"),
    ]:
```
to:
```python
    for cmd, fn, help_ in [
        ("status", cmd_status, "show job detail"),
        ("pause", cmd_pause, "pause a job's timer"),
        ("resume", cmd_resume, "resume a job's timer"),
        ("snapshots", cmd_snapshots, "list snapshots for a job"),
        ("preview", cmd_preview, "list files that would be backed up (.backupignore applied)"),
    ]:
```

- [ ] **Step 4: Run to verify it passes**

Run: `PYTHONPATH=src python -m pytest tests/test_cli.py -v`
Expected: PASS (existing + 2 new).

- [ ] **Step 5: Run the full suite**

Run: `PYTHONPATH=src python -m pytest -q`
Expected: PASS (all files).

- [ ] **Step 6: Update README** — in `README.md`

(a) Add to the usage block (after the `backup snapshots ...` line):
```markdown
backup preview important-project     # list files that would be backed up (.backupignore applied)
```

(b) Add a section after the "Destination integrity" section:
```markdown
### Ignoring files (.backupignore)

Drop a `.backupignore` file in the source directory — or in any subdirectory,
like `.gitignore` — to skip files from the backup. Each line is an exclude
pattern; rsync's globbing applies (`*`, `**`, `?`, `[...]`; a leading `/`
anchors to that directory; a trailing `/` matches directories only; `#` lines
are comments). Example:

```
# build artifacts
*.log
__pycache__/
/node_modules
```

Preview what a run would copy (nothing is written) with
`backup preview <name>`. Because snapshots mirror the source, newly-ignored
files disappear from new snapshots; older snapshots keep them. The
`.backupignore` files are backed up themselves unless you list them too.
```

- [ ] **Step 7: Commit**

```bash
git add src/backup/cli.py tests/test_cli.py README.md
git commit -m "feat: backup preview command and .backupignore docs"
```

---

## Self-Review Notes

- **Spec coverage:** nested `.backupignore` honoring (Task 1, filter in `run_backup` + nested test); `preview` (Task 1 `preview_backup` + Task 2 CLI); writes-nothing + missing-source handling (Task 1 tests); README (Task 2). All spec sections map to a task.
- **Type consistency:** `IGNORE_FILTER: List[str]`, `preview_backup(job) -> List[str]`, `cmd_preview(args) -> int` used identically across tasks; the filter is passed as two argv tokens everywhere.
- **No placeholders:** every step has complete code.
- **Edge cases:** missing source → `[]` / CLI error; rsync error → `[]`; root `.`/`./` lines filtered; preview never touches the real destination (temp dir, no `--delete`, removed in `finally`).
```
