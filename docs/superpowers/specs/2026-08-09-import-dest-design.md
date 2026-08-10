# Disaster-recovery import design

Date: 2026-08-09
Status: approved

## Problem

After a system loss the backup drive survives, but the new install's database
is empty: the tool has no way to register existing job directories so their
snapshots can be browsed and restored. Users need to import either a single
job directory or a centralized destination holding many job directories, and
the tool must state clearly which of the two forms it detected.

## Command

    backup import <path>

Detection (announced explicitly in the output):

- `<path>/snapshots/` exists → **job directory** form; import that one job.
  Output: `importing job directory: <path>`
- Otherwise, immediate children containing `snapshots/` → **destination
  directory** form; import each child.
  Output: `importing destination directory: <path> — found N job
  directories: a, b, c`
- Neither → error describing what a job directory looks like. Two mistakes
  get targeted hints: pointing at a `snapshots/` directory itself, or at a
  timestamped snapshot directory ("pass the job directory above it").

No forcing flags: the two forms are structurally unambiguous.

## Metadata recovery

Per job directory, read `.backup-meta.json` (written by every successful
backup): recover `job_id`, `source`, and `last_snapshot`. `last_snapshot` is
kept only if that snapshot still exists on disk; otherwise it is cleared (and
a warning printed) so the next run adopts instead of blocking. Without a
marker: name from the directory name, a `(unknown:<name>)` source placeholder
(unique per job — `(source, dest)` is UNIQUE and a destination may hold
several marker-less job dirs; not a real path, so unarchive stays refused
until `edit --source`), and no job_id (assigned on next run by the existing
legacy-adopt path).

The job name is ALWAYS the directory name — `job_dir()` derives the on-disk
path from `dest/name`, so a marker name that disagrees with the directory
name cannot be honored. dest is the parent directory of the job dir.

Schedule and retention lived only in the lost database: defaults are used
(`daily@02:00`, keep 7); the user can `backup edit` before unarchiving.

On-disk snapshot names are recorded in the snapshots table at import (same
adoption behavior `snapshot_status` already applies lazily).

Import never writes to the backup drive.

## Imported state

Jobs are imported **archived** (`archived_reason: "imported"`): no timer is
installed, nothing starts running against old snapshots, and browsing /
restore work immediately in the TUI and CLI. `unarchive` (which already
refuses when the source directory is missing) turns the job live again.

Name collision with an existing job: skip that directory with a message.
Summary line: `imported N job(s), skipped M`.

## edit --source

To close the recovery loop, `backup edit <name> --source <dir>` updates the
source path (new machine, new location). It validates that the new source
exists and that the job's dest is not inside it, refreshes the destination
marker so integrity verification keeps passing, and reinstalls the timer
units for non-archived jobs (units embed the source path).

## Tests

- scan: job form, dest form, snapshots-dir hint, snapshot-dir hint, no-match
  error
- import: marker recovery, missing marker, marker pointing at a lost
  snapshot, collision skip, archived state, snapshots-table rows
- CLI: form announcement in output, summary counts, error paths
- edit --source: update + marker refresh, missing dir refusal, recurse check
