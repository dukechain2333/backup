# TUI + snapshot tracking + archive design

Date: 2026-08-09

## Goal

An interactive `backup tui` for managing jobs, plus the plumbing it needs:
per-snapshot tracking in the DB (to detect *lost* snapshots), job archiving
(manual + automatic when the source disappears), and in-TUI restore.

## TUI layout (curses, stdlib — keeps the zero-dependency install)

Three Miller columns over one status/help bar:

```
┌ Tasks ──────────┬ Destinations ────────────┬ Snapshots ──────────────┐
│ ACTIVE          │ /mnt/backups   ok        │ 2026-08-09_02-00-01     │
│ > important-pro │ /media/usb     blocked   │ 2026-08-08_02-00-00     │
│   notes         │                          │ 2026-08-07_02-00-00 LOST│
│ ARCHIVED        │                          │                         │
│   old-thesis    │                          │                         │
└─────────────────┴──────────────────────────┴─────────────────────────┘
 [r]estore [a]rchive [u]narchive [d]elete [g]refresh [q]uit
```

- **Tasks (left)** — jobs grouped by *source path*: one task per source, so a
  folder backed up to several destinations is a single entry. Two sections:
  ACTIVE and ARCHIVED. An archived task whose snapshots cannot be found at any
  destination is flagged `archive lost` (red).
- **Destinations (middle)** — one row per job of the selected task: dest path,
  job name, state (active/paused/blocked/archived), schedule, last run.
  A destination whose snapshot tree is missing shows `lost` (red).
- **Snapshots (right)** — union of DB-recorded and on-disk snapshots for the
  selected destination, newest first. In DB but missing on disk → `LOST` (red).
  On disk but unknown to the DB (pre-feature snapshots) → silently backfilled
  into the DB so future disappearance is detected.

Keys: `←/→`/`h/l`/Tab move between columns, `↑/↓`/`j/k` move in a column,
`Enter` drills right, `r` restore, `a` archive, `u` unarchive, `d` delete an
archived task, `g` refresh, `q` quit. Destructive actions confirm in the
status bar.

## Snapshot tracking

New table `snapshots(job_name, snapshot, created_at, PRIMARY KEY(job_name,
snapshot))`. `runner.run_backup` records each snapshot it creates; `_prune`
forgets the rows it deletes; `db.remove_job` and job rename cascade. *Lost* =
row exists, directory doesn't.

## Archive

New nullable job columns `archived_at`, `archived_reason`.

- **Archive** (TUI `a`, CLI `backup archive <name>`): remove the systemd units
  (timer stops firing) and stamp `archived_at`. Snapshots are left untouched.
  `backup run` refuses archived jobs (without recording a run), `run --all`
  skips them, and `edit` will not reinstall an archived job's timer.
- **Auto-archive**: on TUI load/refresh, any non-archived job whose source
  directory no longer exists is archived with reason `source missing`. Since
  the trigger may be transient (an unmounted drive), auto-archive only
  *disables* the timer instead of deleting the unit files.
- **Unarchive** (TUI `u`, CLI `backup unarchive <name>`): requires the source
  directory to exist again; reinstalls units and clears the archive fields.
- **Delete** (TUI `d`, archived tasks only): removes the job records; the user
  chooses keep-snapshots or purge.

Archiving/deleting a *task* in the TUI applies to all of the source's jobs.

## Restore

`r` restores the selected snapshot (or the newest present one when pressed on
a task/destination) with `rsync -a snapshot/ target/`. The target path is
editable in a prompt, prefilled with:

- the original source path, when that directory still exists;
- `~/<source basename>`, when it doesn't (typical for archived tasks).

Restoring a LOST snapshot is refused.

## Safety guards (added after the safety review)

- **Shrink guard** (`runner.run_backup`): after rsync but before promoting the
  new snapshot, its file count is compared with the previous snapshot's. A
  drop to zero — or below 10% for trees of ≥20 files — aborts the run and
  blocks the job instead of rotating good snapshots away. Root cause this
  defends against: a source that is an empty mount point whose drive is
  absent. `--force` overrides.
- **Snapshot fingerprints** (`snapshots.file_count/total_bytes`): recorded at
  creation; `ops.verify_snapshot` re-counts on demand (TUI `v`, CLI
  `backup verify`) and reports `ok` / `suspect` / `baseline` / `lost`.
  Fingerprints catch structural damage, not same-size bit-rot.
- **Restore overwrite confirmation** (`ops.count_overwrites` + TUI): restoring
  into an existing non-empty directory states how many files will be
  overwritten and asks a second time.
- **Purge identity check** (`ops.delete_job`): purging refuses when the
  destination marker's `job_id` differs from the job's — a different disk
  mounted at the destination path must never be wiped.

## Code layout

- `db.py` — columns + snapshots table + cascades.
- `runner.py` — record/forget snapshots, refuse archived jobs.
- `ops.py` (new) — pure-ish operations shared by CLI and TUI: task grouping,
  snapshot status, archive/unarchive/delete, auto-archive, restore.
- `tui.py` (new) — curses front-end; all decisions delegated to `ops`.
- `cli.py` — `tui`, `archive`, `unarchive` subcommands; `list` shows archived.
