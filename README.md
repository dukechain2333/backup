# backup

Per-directory backups for Linux: register a folder, pick a schedule, and a
systemd **user timer** takes periodic **rsync hard-link snapshots** into a local
destination — keeping the most recent N and pruning the rest. Unchanged files
are hard-linked between snapshots, so each snapshot is a full browsable copy
that costs almost no extra disk space.

## Requirements

- Linux with `systemd` (user instance), `rsync`, and `python3` (3.9+).
- No root required; installs under `~/.local`.

## Install

```bash
git clone <repo-url> backup-tool
cd backup-tool
bash install.sh
```

The installer puts `backup` on your `PATH` (`~/.local/bin`) and enables linger so
timers run even when you are logged out.

## Usage

```bash
# Set a default destination once, so you don't repeat --dest:
backup config --default-dest /mnt/backups
# Or keep several — `backup add` then creates one job per destination:
backup config --add-default-dest /media/usb
backup config --remove-default-dest /media/usb
backup config               # show current settings

# In the folder you want to back up — source defaults to the current directory,
# dest defaults to the configured default-dest:
cd ~/important-project
backup add --schedule daily@02:00
# (or override either: backup add --source /some/dir --dest /other/disk)

backup tui                  # interactive dashboard (see below)
backup list                 # see all jobs, state, last/next run
backup status important-project
backup run important-project    # snapshot one job now
backup run --all                # snapshot every job now (sequentially)
backup logs important-project        # view this job's log (last 40 lines)
backup run important-project --force # override an integrity block & re-baseline
backup pause important-project  # stop future runs
backup resume important-project
backup snapshots important-project
backup verify important-project      # check snapshots against their recorded fingerprints
backup preview important-project     # list files that would be backed up (.backupignore applied)
backup edit important-project --keep 14 --schedule weekly@sun:03:00
backup restore important-project --to /tmp/recovered
backup archive important-project     # stop the timer, keep all snapshots
backup unarchive important-project   # reactivate (source must exist again)
backup remove important-project           # keep snapshots
backup remove important-project --purge   # also delete snapshots
```

### TUI

`backup tui` opens a three-column dashboard:

- **Tasks** — jobs grouped by source folder, so one folder backed up to
  several destinations is a single entry. Active and archived tasks are
  listed separately; an archived task whose snapshots cannot be found
  anywhere is flagged **LOST** in red.
- **Destinations** — every destination of the selected task, with its state
  (active/paused/blocked/archived), snapshot count, and lost-snapshot count.
- **Snapshots** — the selected destination's snapshots, checked against the
  database: a snapshot the DB recorded but that is missing on disk is marked
  **LOST** in red. (On-disk snapshots the DB doesn't know about — e.g. from
  an older version — are adopted silently.)

Keys: arrows / `hjkl` / Tab to navigate, `r` restore, `v` verify snapshot
fingerprints, `a` archive, `u` unarchive, `d` delete an archived task
(choosing keep or purge snapshots), `g` refresh, `q` quit.

**Restore** (`r`) copies the selected snapshot — or the newest one present,
when pressed on a task/destination — to an editable target path, prefilled
with the original source folder (or `~/<folder>` if the source no longer
exists, as for archived tasks). LOST snapshots cannot be restored.

**Archiving** stops the job's timer but never touches its snapshots. A job
whose source folder has disappeared is archived automatically the next time
the TUI loads (only its timer is disabled, so plugging the drive back in and
pressing `u` restores everything). Archived tasks can still be restored
from, unarchived once the source exists again, or deleted (with or without
their snapshots). Archived jobs are skipped by `backup run --all`, and
unarchiving re-enables the timer even if the job was paused before it was
archived.

**Renaming a job** (`--rename`) moves its existing snapshot tree to the new name
automatically. **Changing `--dest`** is refused while snapshots already exist, to
prevent orphaning them — remove and re-add the job at the new destination, or move
the snapshot directory manually first.

### Schedules

`hourly` · `daily@HH:MM` · `weekly@dow:HH:MM` (dow = mon..sun) · `every:Nh` ·
`every:Nm`. For full control pass a raw systemd expression via the timer (see
`man systemd.time`).

### Safety guards

- **Shrink guard** — if a new snapshot would contain no files (or under 10% of
  the previous snapshot's files, for trees of 20+ files), the run is refused
  and the job marked blocked instead of rotating good snapshots away. This
  protects against the classic silent killer: a source directory that is an
  *empty mount point* because its drive isn't attached. Emptying a folder on
  purpose? `backup run <name> --force` confirms it.
- **Snapshot fingerprints** — every snapshot records its file count and total
  size. `backup verify <name>` (or `v` in the TUI) re-counts and flags any
  snapshot that changed after creation as **suspect** — snapshots are
  immutable, so a mismatch means filesystem corruption, manual edits, or a
  partial deletion. Note: hard-linked history means silent bit-rot inside an
  unchanged-size file is *not* detected — fingerprints catch structural
  damage, not flipped bits.
- **Overwrite confirmation** — a TUI restore into an existing, non-empty
  directory first reports how many files would be overwritten and asks again.
- **Purge identity check** — `remove --purge` and the TUI purge refuse to
  delete a snapshot tree whose marker belongs to a different job (the classic
  cause: a different disk mounted at the same path).

### Destination integrity

Each job writes a small marker (`<dest>/<name>/.backup-meta.json`) recording its
identity and last snapshot. Before every run, `backup` checks the destination
still matches — same job, same source, and the recorded last snapshot still
present. If it doesn't (wrong/unmounted drive, wiped or replaced snapshots), the
run is refused and the job is marked **blocked**; scheduled runs keep refusing
until you reconcile. Inspect with `backup logs <name>` and, once you're sure the
destination is correct, re-baseline with `backup run <name> --force`.

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

With multiple **default** destinations configured (`--add-default-dest`),
`backup add` without `--dest` does this fan-out automatically: one job per
default destination, named `<base>-<destination-dir>` (e.g. `proj-nas`,
`proj-usb`), where `<base>` is `--name` or the folder name. Destinations the
source is already backed up to are skipped with a note.

### Where things live

- Job registry: `~/.config/backup/jobs.db`
- Logs: `~/.local/state/backup/logs/<name>.log`
- Timers: `~/.config/systemd/user/backup-<name>.timer`
- Snapshots: `<dest>/<name>/snapshots/<timestamp>/`, with a `latest` symlink.

## Uninstall

```bash
bash uninstall.sh           # remove the CLI
bash uninstall.sh --purge   # also remove all jobs, timers, config, and state
```

Snapshots already written to your destinations are never deleted automatically.
