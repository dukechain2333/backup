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
backup config               # show current settings

# In the folder you want to back up — source defaults to the current directory,
# dest defaults to the configured default-dest:
cd ~/important-project
backup add --schedule daily@02:00
# (or override either: backup add --source /some/dir --dest /other/disk)

backup list                 # see all jobs, state, last/next run
backup status important-project
backup run important-project    # snapshot one job now
backup run --all                # snapshot every job now (sequentially)
backup pause important-project  # stop future runs
backup resume important-project
backup snapshots important-project
backup edit important-project --keep 14 --schedule weekly@sun:03:00
backup restore important-project --to /tmp/recovered
backup remove important-project           # keep snapshots
backup remove important-project --purge   # also delete snapshots
```

**Renaming a job** (`--rename`) moves its existing snapshot tree to the new name
automatically. **Changing `--dest`** is refused while snapshots already exist, to
prevent orphaning them — remove and re-add the job at the new destination, or move
the snapshot directory manually first.

### Schedules

`hourly` · `daily@HH:MM` · `weekly@dow:HH:MM` (dow = mon..sun) · `every:Nh` ·
`every:Nm`. For full control pass a raw systemd expression via the timer (see
`man systemd.time`).

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
