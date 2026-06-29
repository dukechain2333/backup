# `backup` — Per-Directory Snapshot Backup CLI

**Status:** Approved design (2026-06-28)
**Author:** William
**Scope:** Single implementation plan

## 1. Purpose

A per-user command-line tool, installed as the system command `backup`, that lets a
user register directories as recurring backup *jobs*. Each job is driven by its own
**systemd user timer** that periodically takes an **rsync hard-link snapshot** of the
source directory into a local destination, then prunes old snapshots down to a
retention limit.

The tool must let the user:

- Add the current directory (or an explicit path) to the set of backed-up directories.
- Define the backup time / period (schedule).
- View the registered directories and their status.
- Remove a registered directory.
- Manage each backup task: pause, resume, cancel (remove), and run on demand.

### Locked decisions

| Decision | Choice |
|---|---|
| Language | Python 3 (stdlib only; package under `src/backup/`) |
| Scheduling backend | systemd **user** timers (`systemctl --user`) |
| Backup format | rsync incremental **hard-link snapshots** |
| Destination | Local paths only |
| Install scope | Per-user (`~/.local/bin`, `~/.config`), no root; linger enabled |
| Retention | Keep most-recent **N** snapshots (default 7), per-job configurable |
| Schedule input | Friendly presets + raw `OnCalendar` expression |
| Command name | `backup` |

## 2. Command Surface (UX)

All state-changing commands print a clear confirmation line and exit non-zero on error.

| Command | Behavior |
|---|---|
| `backup add [--source PATH] [--dest PATH] [--schedule SPEC] [--keep N] [--name NAME]` | Register a job. Defaults `--source` to the current working directory. `--dest` is required on first add for that source. `--name` defaults to a slug derived from the source basename; must be unique. |
| `backup list` | Table of all jobs: name, source, dest, schedule (human), keep, state (active/paused), last run + status, next run. |
| `backup status <name>` | Detailed view for one job: source/dest, schedule, retention, timer state, next run, last run + status, and the tail of the job log. |
| `backup remove <name> [--purge]` | Cancel the job: stop+disable+delete its systemd units and remove the DB row. `--purge` also deletes the snapshot tree on disk. Without `--purge`, snapshots are left intact. |
| `backup pause <name>` | Disable + stop the job's timer (no future runs). DB row retained. |
| `backup resume <name>` | Re-enable + start the job's timer. |
| `backup run <name>` | Trigger a snapshot immediately (foreground; streams progress). |
| `backup edit <name> [--schedule SPEC] [--keep N] [--dest PATH] [--rename NEW]` | Modify a job; regenerate and reload its timer/service as needed. |
| `backup snapshots <name>` | List snapshots for the job: timestamp and on-disk size, newest first. |
| `backup restore <name> [--snapshot TS] [--to PATH]` | Restore a snapshot (default: newest) to `--to` (default: a new `restore-<TS>` dir next to the source; never overwrites the live source without an explicit `--to`). |

Internal (not user-facing): `backup _run <name>` — the entry point invoked by the
systemd service unit. Performs the snapshot + prune + status recording.

### Schedule spec (`--schedule`)

Friendly presets that map to a systemd `OnCalendar=` value:

| Preset | OnCalendar |
|---|---|
| `hourly` | `hourly` |
| `daily@HH:MM` (e.g. `daily@02:00`) | `*-*-* HH:MM:00` |
| `weekly@DOW:HH:MM` (e.g. `weekly@sun:03:00`) | `Sun *-*-* HH:MM:00` |
| `every:Nh` (e.g. `every:6h`) | `*-*-* 00/N:00:00` (every N hours from midnight) |
| `every:Nm` (e.g. `every:30m`) | `*-*-* *:00/N:00` (every N minutes) |

Power users may pass `--oncalendar "<raw systemd expr>"` to bypass presets. Exactly one
of `--schedule` / `--oncalendar` is accepted. Invalid expressions are validated via
`systemd-analyze calendar "<expr>"` before the job is saved.

## 3. Architecture

### On-disk layout

```
~/.local/bin/backup                       # symlink to the installed entry point
~/.local/share/backup/                     # installed package source
~/.config/backup/jobs.db                   # SQLite: source of truth for job metadata
~/.local/state/backup/logs/<name>.log      # per-job run log (append)
~/.config/systemd/user/
    backup-<name>.service                  # Type=oneshot, ExecStart=<bin> _run <name>
    backup-<name>.timer                    # OnCalendar=..., Persistent=true
```

### Source of truth split

- **SQLite (`jobs.db`)** holds durable job metadata: `name` (PK), `source`, `dest`,
  `oncalendar`, `schedule_human`, `keep`, `created_at`, `last_run_at`, `last_status`,
  `last_message`.
- **systemd is queried live** for the dynamic state (active vs paused, next run time)
  via `systemctl --user is-enabled` / `list-timers`. Nothing about enabled state is
  duplicated in the DB, so the two can't drift.

### Lifecycle operations

- **add**: validate inputs → insert DB row → render `.service` + `.timer` from templates
  → `systemctl --user daemon-reload` → `enable --now backup-<name>.timer`.
- **pause/resume**: `systemctl --user disable --now` / `enable --now` the timer.
- **remove**: `systemctl --user disable --now` → delete unit files → `daemon-reload` →
  delete DB row → (optional `--purge`) remove snapshot tree.
- **edit**: update DB → re-render units → `daemon-reload` → re-`enable --now` if active.

### Snapshot layout at destination

```
<dest>/<name>/
  snapshots/
    2026-06-28_02-00-00/       # full, browsable copy of the source
    2026-06-27_02-00-00/       # unchanged files hard-linked to the prior snapshot
  latest -> snapshots/2026-06-28_02-00-00
```

A run does, roughly:

```
rsync -a --delete \
  --link-dest="<dest>/<name>/snapshots/<previous>" \
  "<source>/" "<dest>/<name>/snapshots/<new-timestamp>.partial/"
mv  ".../<new-timestamp>.partial"  ".../<new-timestamp>"
ln -sfn  "snapshots/<new-timestamp>"  "<dest>/<name>/latest"
```

The `.partial` → final rename makes each snapshot atomic: an interrupted run never
leaves a half-written directory that later runs would `--link-dest` against. Retention
prune then deletes all but the newest `keep` finalized snapshot directories.

### Python module layout (`src/backup/`)

| Module | Responsibility |
|---|---|
| `cli.py` | argparse parsing + subcommand dispatch; the `backup` entry point. |
| `db.py` | SQLite open/migrate + typed CRUD for the `jobs` table. |
| `schedule.py` | Parse `--schedule` presets → `OnCalendar`; validate via `systemd-analyze`. |
| `units.py` | Render `.service`/`.timer` templates; thin wrapper around `systemctl --user`. |
| `runner.py` | Perform the rsync snapshot, atomic finalize, `latest` symlink, prune, and status write. |
| `paths.py` | Resolve config/state/unit dirs (honoring `XDG_*`); single place for path policy. |

The `systemctl`/`systemd-analyze` calls live behind small functions in `units.py` /
`schedule.py` so tests can fake them.

## 4. Error Handling & Edge Cases

- **add validation**: source must exist and be a directory; dest must be creatable and
  writable; dest must **not** be inside source (reject recursive backup); name must be a
  valid slug and unique; reject a duplicate (same source already registered) with a clear
  message pointing at the existing job.
- **run failures**: rsync exit code is captured. Codes 0 and 24 ("some files vanished
  during transfer") are treated as success; anything else records `last_status=failed`
  with the message, writes to the per-job log + journald, and exits non-zero so systemd
  marks the unit failed.
- **overlap**: systemd serializes a given oneshot unit, so two runs of the same job never
  overlap. No extra locking needed.
- **missing dest at run time** (e.g. unmounted drive): fail fast with a clear message
  rather than silently creating an empty snapshot on the wrong filesystem; record failure.
- **partial snapshots**: `.partial` directories are ignored by retention/`--link-dest`
  selection and cleaned up on the next successful run.

## 5. Installation

`install.sh` (no root required):

1. Copy `src/backup/` to `~/.local/share/backup/` and create the `~/.local/bin/backup`
   symlink (entry point).
2. Ensure `~/.local/bin` is on `PATH`; if not, append an export to the user's shell rc
   and warn that a new shell / `source` is needed.
3. Create `~/.config/backup/` and `~/.local/state/backup/logs/`.
4. `loginctl enable-linger "$USER"` so user timers run even when not logged in (warn if
   it fails, e.g. no privileges — jobs then only run while logged in).
5. Initialize the SQLite DB and print a getting-started hint.

`uninstall.sh`: remove the symlink and installed package. With `--purge`, also stop +
disable + delete every `backup-*` user timer/service, `daemon-reload`, and delete the
config/state dirs (snapshots on backup destinations are never auto-deleted).

## 6. Testing (pytest)

- **runner**: real temp source/dest dirs + real `rsync`; assert snapshot contents,
  hard-link sharing of unchanged files across snapshots, atomic finalize, `latest`
  symlink target, and retention prune (keep N).
- **schedule**: preset → `OnCalendar` mappings; rejection of malformed specs.
- **db**: create/migrate, insert, query, update status, delete, uniqueness constraint.
- **units**: rendered `.service`/`.timer` content (string assertions); lifecycle calls
  routed through a faked `systemctl` wrapper.
- **cli**: argument parsing and validation errors (source missing, dest inside source,
  duplicate name) using temp dirs and faked systemd layer.
- **smoke** (optional, marked): full end-to-end add → `_run` → snapshots present.

## 7. Repository Layout

```
backup-tool/
  README.md                # install, usage, examples, how snapshots/restore work
  LICENSE                  # MIT
  pyproject.toml           # metadata + console_scripts entry point
  install.sh
  uninstall.sh
  src/backup/{__init__,cli,db,schedule,units,runner,paths}.py
  tests/test_{runner,schedule,db,units,cli}.py
  docs/superpowers/specs/2026-06-28-backup-design.md
```

## 8. Out of Scope (YAGNI)

- Remote/SSH destinations, cloud targets, encryption.
- Compression-based archives (tar) and dedup engines (restic/borg).
- System-wide (root) installation.
- A daemon process — systemd timers cover scheduling.
- A TUI/GUI.

These are deliberately excluded from this iteration; the snapshot layout and DB schema
leave room to add remote destinations later without redesign.
