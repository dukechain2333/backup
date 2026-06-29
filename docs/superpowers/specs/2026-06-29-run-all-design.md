# Run All Jobs

**Status:** Approved design (2026-06-29)
**Builds on:** [2026-06-28-backup-design.md](2026-06-28-backup-design.md)

## Purpose

A single command to immediately back up every registered job, instead of running
them one name at a time.

## Command

Extend the existing `run` command:
- `backup run <name>` — run one job (unchanged).
- `backup run --all` — run every job.
- `backup run` (neither name nor `--all`) — error: `specify a job name or --all`.
- `backup run <name> --all` (both) — error: `give a job name or --all, not both`.

The `name` argument becomes optional (`nargs="?"`); add a `--all` store-true flag.

## `--all` behavior

- Iterate jobs in `db.list_jobs` order (alphabetical by name); run each
  **sequentially** via `runner.run_backup` (no concurrency — avoids disk
  contention and keeps output readable).
- Print one line per job: `<name>: <status>: <message>`.
- Continue on failure — one job failing does not stop the others.
- Print a final summary line: `<N> ok, <M> failed`.
- Exit code 0 only if every job succeeded; non-zero if any failed (so scripts
  can detect partial failure).
- Include paused jobs — this is a user-initiated "back up everything now", not
  the scheduler.
- If there are no jobs, print `no backup jobs registered.` and exit 0.

## Testing

- `run --all` runs all jobs and reports a summary (real rsync, temp dirs).
- `run --all` continues past a failing job and exits non-zero, while the healthy
  job still produced a snapshot.
- `run` with neither name nor `--all` errors.
- `run <name> --all` (both) errors.
- `run <name>` still runs exactly one job (existing behavior preserved).

## Out of scope

Parallel execution, filtering/globbing job subsets, a separate `run-all` command.
