"""Operations shared by the CLI and the TUI.

Everything here works on `db.Job` values and a live connection; the front-ends
only decide when to call and how to render the result.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from . import db, integrity, paths, runner, units

_RSYNC_OK = {0, 24}


def human_size(n) -> str:
    if n is None:
        return "?"
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024:
            return "%d%s" % (n, unit)
        n //= 1024
    return "%dP" % n


@dataclass
class SnapshotInfo:
    name: str
    on_disk: bool
    in_db: bool
    file_count: Optional[int] = None      # recorded at creation
    total_bytes: Optional[int] = None
    verdict: Optional[str] = None         # ok|suspect|baseline|lost, once verified

    @property
    def lost(self) -> bool:
        return self.in_db and not self.on_disk


@dataclass
class Task:
    """All jobs backing up one source directory."""
    source: str
    jobs: List[db.Job] = field(default_factory=list)

    @property
    def label(self) -> str:
        return Path(self.source).name or self.source

    @property
    def archived(self) -> bool:
        return all(j.archived_at is not None for j in self.jobs)


def group_tasks(jobs: List[db.Job]) -> List[Task]:
    by_source = {}
    for job in jobs:
        by_source.setdefault(job.source, Task(source=job.source)).jobs.append(job)
    return sorted(by_source.values(), key=lambda t: (t.label, t.source))


def snapshot_status(conn, job: db.Job) -> List[SnapshotInfo]:
    """Union of DB-recorded and on-disk snapshots, newest first.

    On-disk snapshots the DB doesn't know about (created before snapshot
    tracking existed) are backfilled into the DB, so that their later
    disappearance is detected as a loss.
    """
    on_disk = {p.name for p in runner.list_snapshots(job)}
    in_db = set(db.list_snapshot_names(conn, job.name))
    for name in sorted(on_disk - in_db):
        db.record_snapshot(conn, job.name, name)
        in_db.add(name)
    stats = db.snapshot_stats_map(conn, job.name)
    result = []
    for n in sorted(on_disk | in_db, reverse=True):
        count, size = stats.get(n, (None, None))
        result.append(SnapshotInfo(
            name=n, on_disk=n in on_disk, in_db=n in in_db,
            file_count=count, total_bytes=size))
    return result


def archive_lost(job: db.Job) -> bool:
    """True when no snapshot of this job can be found on disk."""
    return not runner.list_snapshots(job)


def job_state(job: db.Job) -> str:
    """archived > blocked > active/paused — the one state ladder for all UIs."""
    if job.archived_at:
        return "archived"
    if job.blocked_reason:
        return "blocked"
    return "active" if units.is_active(job.name) else "paused"


def archive_job(conn, job: db.Job, reason: str = "manual",
                keep_units: bool = False) -> None:
    """Stop the job's timer but leave every snapshot untouched.

    With keep_units the timer is only disabled, not deleted — used by
    auto-archive, which may fire on a transiently missing source (e.g. an
    unmounted drive) and must stay easy to reverse.
    """
    if keep_units:
        units.disable_units(job.name)
    else:
        units.remove_units(job.name)
    db.update_job(
        conn, job.name,
        archived_at=datetime.now().isoformat(timespec="seconds"),
        archived_reason=reason,
    )


def unarchive_job(conn, job: db.Job) -> None:
    if not Path(job.source).is_dir():
        raise ValueError("source directory missing: %s" % job.source)
    units.install_units(job.name, job.oncalendar,
                        paths.backup_executable(), job.source)
    db.update_job(conn, job.name, archived_at=None, archived_reason=None)


def auto_archive_missing_sources(conn, jobs: List[db.Job]) -> List[str]:
    """Archive every non-archived job whose source directory vanished."""
    archived = []
    for job in jobs:
        if job.archived_at is None and not Path(job.source).is_dir():
            archive_job(conn, job, reason="source missing", keep_units=True)
            job.archived_at = db.get_job(conn, job.name).archived_at
            job.archived_reason = "source missing"
            archived.append(job.name)
    return archived


def delete_job(conn, job: db.Job, purge: bool = False) -> None:
    if purge:
        # Never purge a directory that identifies as some other job's data —
        # the classic cause is a different disk mounted at the same path.
        marker = integrity.read_marker(job)
        if (marker is not None and job.job_id
                and marker.get("job_id") != job.job_id):
            raise ValueError(
                "refusing to purge %s: its marker belongs to a different job "
                "(wrong disk mounted at %s?); delete it manually if intended"
                % (runner.job_dir(job), job.dest))
    units.remove_units(job.name)
    db.remove_job(conn, job.name)
    if purge:
        shutil.rmtree(runner.job_dir(job), ignore_errors=True)


def verify_snapshot(conn, job: db.Job, snapshot: str) -> Tuple[str, str]:
    """Compare a snapshot's on-disk state against its recorded fingerprint.

    Returns (verdict, message); verdict is one of:
      lost      directory missing entirely
      baseline  no fingerprint was recorded — current state adopted as one
      ok        file count and total size match the record
      suspect   they differ: files vanished or changed inside an immutable
                snapshot (filesystem corruption, manual edits, partial delete)
    """
    snap_dir = runner.job_dir(job) / "snapshots" / snapshot
    if not snap_dir.is_dir():
        return "lost", "missing on disk"
    count, size = runner.tree_stats(snap_dir)
    recorded = db.get_snapshot_stats(conn, job.name, snapshot)
    if recorded is None or recorded[0] is None:
        db.record_snapshot(conn, job.name, snapshot)
        db.set_snapshot_stats(conn, job.name, snapshot, count, size)
        return "baseline", ("no recorded fingerprint; adopted current state "
                            "(%d files, %s)" % (count, human_size(size)))
    rec_count, rec_size = recorded
    if count != rec_count or (rec_size is not None and size != rec_size):
        return "suspect", ("recorded %d files / %s, found %d files / %s"
                           % (rec_count, human_size(rec_size),
                              count, human_size(size)))
    return "ok", "%d files, %s" % (count, human_size(size))


def count_overwrites(job: db.Job, snapshot: str, target: Path) -> int:
    """How many files a restore would overwrite in an existing target."""
    if not target.is_dir():
        return 0
    snap_dir = runner.job_dir(job) / "snapshots" / snapshot
    overwrites = 0
    for root, _dirs, names in os.walk(snap_dir):
        rel = Path(root).relative_to(snap_dir)
        for name in names:
            if (target / rel / name).exists():
                overwrites += 1
    return overwrites


def default_restore_target(job: db.Job) -> Path:
    """Original source path while it exists; ~/<basename> once it is gone."""
    source = Path(job.source)
    if source.is_dir():
        return source
    return Path.home() / (source.name or job.name)


_SNAP_NAME_RE = re.compile(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}")

# Schedule/retention lived only in the lost database; imported jobs get the
# same defaults as `backup add` and stay archived until the user reviews them.
_IMPORT_ONCALENDAR = "*-*-* 02:00:00"
_IMPORT_SCHEDULE_HUMAN = "daily at 02:00"
_IMPORT_KEEP = 7


def _is_job_dir(path: Path) -> bool:
    return (path / "snapshots").is_dir()


def scan_import_path(path: Path) -> Tuple[str, List[Path]]:
    """Classify a path for disaster-recovery import.

    Returns ("job", [path]) for a single job directory (it contains
    `snapshots/`), or ("dest", children) for a destination directory whose
    immediate children are job directories. Raises ValueError — with a
    targeted hint for the two classic mistakes (pointing at `snapshots/`
    itself, or at one timestamped snapshot) — when neither form matches.
    """
    if not path.is_dir():
        raise ValueError("%s is not a directory" % path)
    if _is_job_dir(path):
        return "job", [path]
    if path.name == "snapshots" and _is_job_dir(path.parent):
        raise ValueError(
            "%s is a snapshots directory; pass its parent job directory %s"
            % (path, path.parent))
    if _SNAP_NAME_RE.fullmatch(path.name) and path.parent.name == "snapshots":
        raise ValueError(
            "%s is a single snapshot; pass the job directory %s above it"
            % (path, path.parent.parent))
    children = sorted(
        (p for p in path.iterdir() if p.is_dir() and _is_job_dir(p)),
        key=lambda p: p.name)
    if children:
        return "dest", children
    raise ValueError(
        "%s is neither a job directory (containing snapshots/) nor a "
        "destination directory whose subdirectories contain snapshots/"
        % path)


def import_job_dir(
    conn, job_path: Path, now: Optional[datetime] = None
) -> Tuple[Optional[str], str]:
    """Register an existing on-disk job directory as an archived job.

    Reads the destination marker when present to recover job_id, source and
    last_snapshot; never writes to the backup drive. Returns (name, message),
    with name None when the directory was skipped.
    """
    now = now or datetime.now()
    # The name must be the directory name: job_dir() locates data at
    # dest/name, so a marker that disagrees cannot be honored.
    name = job_path.name
    if db.get_job(conn, name) is not None:
        return None, "a job named %r already exists; skipped %s" % (
            name, job_path)

    marker = integrity.read_marker(db.Job(
        name=name, source="", dest=str(job_path.parent),
        oncalendar="", schedule_human="", keep=1, created_at="")) or {}

    warning = ""
    last_snapshot = marker.get("last_snapshot")
    if last_snapshot is not None and not (
            job_path / "snapshots" / str(last_snapshot)).is_dir():
        warning = ("; recorded last snapshot %s is missing on disk, "
                   "cleared so the next run re-adopts" % last_snapshot)
        last_snapshot = None

    source = marker.get("source")
    if not isinstance(source, str) or not source:
        # Placeholder must be unique per job: (source, dest) is UNIQUE, and a
        # centralized destination may hold several marker-less job dirs. Not
        # a real path, so unarchive stays refused until 'edit --source'.
        source = "(unknown:%s)" % name
    job = db.Job(
        name=name, source=source,
        dest=str(job_path.parent),
        oncalendar=_IMPORT_ONCALENDAR, schedule_human=_IMPORT_SCHEDULE_HUMAN,
        keep=_IMPORT_KEEP,
        created_at=now.isoformat(timespec="seconds"),
        job_id=marker.get("job_id"),
        last_snapshot=last_snapshot,
        archived_at=now.isoformat(timespec="seconds"),
        archived_reason="imported",
    )
    try:
        db.add_job(conn, job)
    except ValueError as exc:
        return None, "cannot import %s: %s" % (job_path, exc)
    snaps = runner.list_snapshots(job)
    for snap in snaps:
        db.record_snapshot(conn, name, snap.name)
    return name, "imported %r (%d snapshot%s, archived)%s" % (
        name, len(snaps), "" if len(snaps) == 1 else "s", warning)


def restore_snapshot(
    job: db.Job, snapshot: str, target: Path
) -> Tuple[bool, str]:
    snap_dir = runner.job_dir(job) / "snapshots" / snapshot
    if not snap_dir.is_dir():
        return False, "snapshot %s not found on disk (lost?)" % snapshot
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, "cannot create %s: %s" % (target, exc)
    result = subprocess.run(
        ["rsync", "-a", "%s/" % snap_dir, "%s/" % target],
        capture_output=True, text=True,
    )
    if result.returncode not in _RSYNC_OK:
        return False, "rsync failed (code %d): %s" % (
            result.returncode, result.stderr.strip())
    return True, "restored %s -> %s" % (snapshot, target)
