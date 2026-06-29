# Destination Integrity Verification

**Status:** Approved design (2026-06-29)
**Builds on:** [2026-06-28-backup-design.md](2026-06-28-backup-design.md),
[2026-06-29-schema-migration-design.md](2026-06-29-schema-migration-design.md)

## Purpose

Before each backup run, verify that the destination is the *same* place and
*same* lineage as last time, so a swapped/unmounted/wrong drive or a
replaced/tampered snapshot tree can't pollute the backup. On mismatch the run is
refused and the job is latched **blocked** until the user reconciles. The user
can inspect the log and force an override (re-baseline).

## Data model

Three new **nullable** columns on `jobs`, added via the existing `_ADDED_COLUMNS`
migration (old DBs upgrade in place; new DBs get them from the base schema):

- `job_id TEXT` — a UUID identifying the job, generated at `add` time. Legacy
  jobs (NULL) get one assigned on their next run (self-baseline).
- `last_snapshot TEXT` — the timestamp-name of the most recent successful
  snapshot; the recorded lineage tip. NULL = no baseline yet.
- `blocked_reason TEXT` — NULL means not blocked. Non-NULL latches the job:
  every run refuses until cleared.

## Marker file

Written to `<dest>/<name>/.backup-meta.json` (alongside `snapshots/` and
`latest`; rsync only writes inside `snapshots/<ts>/`, so the marker is safe):

```json
{
  "job_id": "<uuid>",
  "name": "<name>",
  "source": "<source abspath>",
  "last_snapshot": "<timestamp or null>",
  "updated_at": "<iso8601>"
}
```

Written/overwritten after every successful run and on forced re-baseline.

## New module: `integrity.py`

- `marker_path(job) -> Path` — `<dest>/<name>/.backup-meta.json`.
- `read_marker(job) -> Optional[dict]` — parse marker, or None if absent/unreadable.
- `write_marker(job, last_snapshot) -> None` — write/replace the marker
  (creating the job dir if needed).
- `verify(job) -> Tuple[bool, Optional[str]]` — returns `(True, None)` if the
  run may proceed, else `(False, reason)`.

### `verify` logic

Let `marker = read_marker(job)`, `has_baseline = job.last_snapshot is not None`.

1. **No baseline yet** (`job.last_snapshot is None`): allowed (first run /
   legacy self-baseline). If a marker exists with a *different* `job_id`, that's
   a mismatch: `"destination already belongs to job <id>"`.
2. **Has baseline:**
   - marker missing → `"destination marker missing (dest moved, unmounted, or wiped?)"`.
   - `marker["job_id"] != job.job_id` → `"destination belongs to a different job (id mismatch)"`.
   - `marker["source"] != job.source` → `"source path changed since last backup"`.
   - recorded `job.last_snapshot` dir not present under `snapshots/` →
     `"recorded last snapshot <x> missing from destination (content changed)"`.
   - `marker["last_snapshot"] != job.last_snapshot` →
     `"destination marker out of sync with records"`.
   - otherwise → OK.

## Runner integration

`run_backup(job, conn=None, now=None, force=False)`:

1. **Blocked latch:** if `job.blocked_reason` and not `force` → log
   `"still blocked: <reason>; run 'backup run <name> --force' to override"`,
   record status `blocked`, return a failed/blocked `RunResult` (exit non-zero).
   Do not run rsync.
2. **Verify:** if not `force`, call `integrity.verify(job)`. On mismatch → set
   `blocked_reason` in the DB, log the reason + journald, record status
   `blocked`, return without running. On OK → continue.
3. **Force:** skip steps 1–2; clear `blocked_reason`; proceed; log
   `"forced run (verification skipped, re-baselined)"`.
4. **Run** the rsync snapshot as today.
5. **On success:** clear `blocked_reason`, set `job.last_snapshot` to the new
   snapshot name in the DB, and `integrity.write_marker(job, new_snapshot)`.

`RunResult` gains a `blocked` status value (distinct from `failed`) so callers
can report it precisely; exit code is non-zero for both.

## CLI

- `backup run <name> [--force]` and `backup run --all [--force]` — `--force`
  overrides verification and the blocked latch, and re-baselines on success.
- `backup logs <name> [--lines N]` — print the job's log file
  (`~/.local/state/backup/logs/<name>.log`), last N lines (default 40); clear
  message if no log exists or job unknown.
- `backup list` — show `blocked` as the state when `blocked_reason` is set
  (precedence: blocked > paused/active).
- `backup status <name>` — show a `blocked: <reason>` line when blocked.
- `cmd_add` generates and stores a `job_id` for new jobs.

## Error handling / edge cases

- Marker unreadable/corrupt JSON → treated as "missing" → mismatch when a
  baseline exists (safe default: refuse rather than risk pollution).
- Marker write failure after a successful snapshot → log a warning; the snapshot
  itself is valid. (Next run may then see an out-of-sync marker and block, which
  the user resolves with `--force`.)
- `force` on a never-run job behaves like a normal first run (establishes
  baseline).
- Verification never runs rsync; a blocked job leaves existing snapshots
  untouched.

## Testing

- `verify`: first-run OK; matching marker OK; each mismatch branch returns the
  right reason (marker missing, id mismatch, source changed, last snapshot
  missing, marker out of sync); foreign-marker-on-first-run mismatch.
- `write_marker`/`read_marker` round-trip; corrupt marker reads as None.
- `run_backup`: blocked latch refuses without force; mismatch sets
  `blocked_reason` and writes no snapshot; success writes marker + records
  `last_snapshot` + clears blocked; `force` re-baselines and clears blocked.
- `cli`: `run --force` clears a blocked job and runs; `logs` prints the log and
  handles missing log/unknown job; `list`/`status` surface `blocked`; `add`
  assigns a `job_id`.
- migration: the three columns appear on an upgraded old DB (covered by the
  `_ADDED_COLUMNS` mechanism).

## Out of scope

Source-side content fingerprinting (detecting a remounted source), encryption,
and any non-additive schema change.
