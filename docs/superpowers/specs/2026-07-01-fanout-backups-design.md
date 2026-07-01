# Fan-out Backups (one source → many destinations)

**Status:** Approved design (2026-07-01)
**Builds on:** [2026-06-28-backup-design.md](2026-06-28-backup-design.md)

## Purpose

Allow the same source directory to be registered as more than one backup job,
each writing to its own destination — so a single folder can be backed up to
multiple locations (e.g. a local disk and an external drive). When a user adds
a second job for a source that is already backed up, remind them of the existing
job(s) and require confirmation before proceeding.

Today this is impossible: the `jobs` table declares `source TEXT NOT NULL
UNIQUE`, and `cmd_add` hard-rejects any re-used source. Every other part of the
system is already keyed by the job `name` (primary key) — the systemd unit runs
`_run {name}`, `_run` looks up by name, and logs, timers, snapshots
(`<dest>/<name>/snapshots/…`) and integrity markers
(`<dest>/<name>/.backup-meta.json`) are all name-scoped. So two jobs that share
a source but differ in name/dest already isolate cleanly. The `source UNIQUE`
constraint is the only blocker.

## Uniqueness rules

- **Same source → different dest:** allowed (this is the feature).
- **Same source → same dest:** blocked. Two jobs writing identical data to the
  same disk (differing only by name) is redundant and almost certainly a
  mistake.

Enforced at the storage layer by a composite unique index on `(source, dest)`,
replacing the old single-column `UNIQUE` on `source`.

## Schema change (`src/backup/db.py`)

- `source` becomes `source TEXT NOT NULL` (drop the column-level `UNIQUE`).
- Add `CREATE UNIQUE INDEX IF NOT EXISTS jobs_source_dest ON jobs(source, dest)`
  to `_SCHEMA` (so fresh databases get it directly).

### Migration for existing databases

SQLite cannot drop a column-level constraint via `ALTER TABLE`, so a database
created by an older version still carries the single-column UNIQUE on `source`.
`_migrate` gains a one-time, idempotent table rebuild:

1. Run the existing additive-column migration first (`_ADDED_COLUMNS`) so every
   column exists before any copy.
2. Detect the legacy constraint: iterate `PRAGMA index_list(jobs)`; for each
   index with `origin == 'u'` (a column-level UNIQUE) inspect
   `PRAGMA index_info(<index>)` — if it covers exactly the single column
   `source`, the legacy constraint is present.
3. If present, rebuild inside a transaction:
   - `CREATE TABLE jobs_new (…)` with the new schema (no `UNIQUE` on `source`).
   - `INSERT INTO jobs_new (<all columns>) SELECT <all columns> FROM jobs`.
   - `DROP TABLE jobs`.
   - `ALTER TABLE jobs_new RENAME TO jobs`.
4. Always `CREATE UNIQUE INDEX IF NOT EXISTS jobs_source_dest ON jobs(source, dest)`.

The rebuild copies the exact `_COLUMNS` list, so all data (including
`job_id`, `last_snapshot`, `blocked_reason`) is preserved. Idempotent: if the
legacy constraint is absent (already rebuilt or freshly created), only the
`CREATE UNIQUE INDEX IF NOT EXISTS` runs, which is a no-op when the index exists.

The `config` table is untouched.

## `db.py` API change

Replace `get_job_by_source(conn, source) -> Optional[Job]` with:

```python
def list_jobs_by_source(conn, source: str) -> List[Job]:
    """All jobs whose source equals `source`, ordered by name."""
```

`get_job_by_source` is used in exactly one place (`cmd_add`), so there are no
other call sites to update.

## `add` flow (`src/backup/cli.py`)

After the existing source/dest resolution, recursion check, name/keep/schedule
validation, and the "a job named X already exists" check:

1. `existing = db.list_jobs_by_source(conn, str(source))`.
2. If any job in `existing` has `dest == str(dest)` → error and exit non-zero:
   `source already backed up to <dest> as job '<name>'`.
3. Else if `existing` is non-empty (fan-out to a new dest):
   - Print a reminder to stderr listing each existing job and its dest, e.g.:
     ```
     note: this source is already backed up:
       - job 'docs' -> /mnt/backups
     ```
   - Confirmation gate:
     - If `args.yes` is set → proceed.
     - Else if stdin is a TTY (`sys.stdin.isatty()`) → prompt
       `Add another backup of this source to <dest>? [y/N] `; proceed only on a
       `y`/`yes` (case-insensitive) answer, otherwise print `aborted.` and exit
       non-zero.
     - Else (non-interactive, no `--yes`) → error:
       `source already registered; re-run with --yes to add another destination`
       and exit non-zero. Never hang waiting for input.
4. Proceed with job creation exactly as today.

### Name-collision hint

The auto-derived name is `slugify(source.name)`. Adding the same source twice
without `--name` collides on that name and hits the existing "a job named X
already exists" check first. When the colliding job shares this source, extend
that error message with a hint: append
`(pass --name to add another backup of the same source)`.

### New CLI argument

`add` gains `--yes` (store_true): skip the duplicate-source confirmation prompt
(and enable adding a duplicate source non-interactively).

## Unchanged components

Integrity (`integrity.py`), the snapshot engine (`runner.py`), timers/units
(`units.py`), `run` / `run --all`, `restore`, `preview`, `pause`/`resume`,
`remove`, `edit` — all are name-scoped and require no changes. Fan-out jobs are
ordinary independent jobs that happen to share a `source` value.

## Error handling

- Duplicate `(source, dest)` is caught by the explicit check in step 2 before
  insert; the composite unique index is a backstop (its `sqlite3.IntegrityError`
  surfaces via `add_job` as a `ValueError`, already handled).
- Non-interactive add without `--yes` never blocks on input.
- Migration runs inside `connect()`; a rebuild failure raises and leaves the
  original table intact (the transaction rolls back).

## Testing

- **Migration:** build a DB with the legacy `source UNIQUE` schema and a row;
  `connect()` upgrades it in place — the row's data is preserved, the legacy
  single-column index is gone, the composite `(source, dest)` index exists, and
  a second job on the same source to a new dest can then be inserted.
  Idempotent: a second `connect()` is a no-op.
- **Fresh DB:** has the composite index and no single-column source UNIQUE.
- **`list_jobs_by_source`:** returns all jobs for a source (0, 1, many), ordered.
- **`add` fan-out (TTY skipped via `--yes`):** same source + new dest creates a
  second independent job; both back up to their own snapshot trees.
- **Duplicate block:** same source + same dest → refused, non-zero, clear
  message, no second job created.
- **Confirm gating:** non-TTY without `--yes` → refused with the re-run hint;
  with `--yes` → proceeds. (TTY prompt logic tested by monkeypatching
  `sys.stdin.isatty` and `input`.)
- **Name-collision hint:** adding same source without `--name` errors with the
  `--name` hint.

## Out of scope

- A command to list/group jobs by shared source (fan-out jobs just appear in
  `backup list` like any other).
- Deduplicating snapshot storage across destinations.
- Changing how a single job targets one dest (each job is still one → one).
