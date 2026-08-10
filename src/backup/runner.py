from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from . import db, integrity, paths

TIMESTAMP_FMT = "%Y-%m-%d_%H-%M-%S"
_RSYNC_OK = {0, 24}  # 24 = some source files vanished during transfer
IGNORE_FILTER = ["--filter", "dir-merge,- .backupignore"]


@dataclass
class RunResult:
    status: str
    message: str
    snapshot: Optional[str]


def job_dir(job: db.Job) -> Path:
    return Path(job.dest) / job.name


def _snapshots_dir(job: db.Job) -> Path:
    return job_dir(job) / "snapshots"


def list_snapshots(job: db.Job) -> List[Path]:
    snaps = _snapshots_dir(job)
    if not snaps.is_dir():
        return []
    dirs = [
        p for p in snaps.iterdir()
        if p.is_dir() and not p.name.endswith(".partial")
    ]
    return sorted(dirs, key=lambda p: p.name)


def tree_stats(path: Path) -> tuple:
    """(file count, total bytes) of a directory tree. Symlinks counted, not
    followed; unreadable entries skipped — this is a fingerprint, not an audit."""
    files = 0
    total = 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            files += 1
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return files, total


# A new snapshot this much smaller than the previous one blocks the run: it is
# far more likely an unmounted/emptied source (or runaway .backupignore) than a
# real cleanup. Small trees are exempt from the ratio check — only total loss
# (0 files after a non-empty snapshot) blocks there.
_SHRINK_MIN_FILES = 20
_SHRINK_RATIO = 0.1


def _log(job: db.Job, message: str) -> None:
    try:
        paths.log_dir().mkdir(parents=True, exist_ok=True)
        logfile = paths.log_dir() / ("%s.log" % job.name)
        with logfile.open("a") as fh:
            fh.write("%s %s\n" % (datetime.now().isoformat(timespec="seconds"), message))
    except OSError:
        pass  # logging must never crash a backup run


def _prune(job: db.Job, conn=None) -> None:
    snaps = list_snapshots(job)
    excess = len(snaps) - job.keep
    for old in snaps[:max(0, excess)]:
        shutil.rmtree(old, ignore_errors=True)
        # Only forget snapshots that are really gone: a failed rmtree (e.g.
        # read-only destination) must not leave a half-deleted directory that
        # the DB no longer tracks — it would be re-adopted as a good snapshot.
        if conn is not None and not old.exists():
            db.forget_snapshot(conn, job.name, old.name)


def run_backup(
    job: db.Job, conn=None, now: Optional[datetime] = None, force: bool = False
) -> RunResult:
    now = now or datetime.now()
    source = Path(job.source)
    dest_base = Path(job.dest)

    if job.archived_at:
        # Refuse without recording a run: an archived job is dormant and its
        # last_run/last_status should keep describing its last real backup.
        msg = ("job is archived (%s); unarchive it to back up again"
               % (job.archived_reason or "manual"))
        _log(job, "archived: %s" % msg)
        return RunResult(status="archived", message=msg, snapshot=None)

    if job.blocked_reason and not force:
        return _finish(
            job, conn, now, "blocked",
            "still blocked: %s; run 'backup run %s --force' to override"
            % (job.blocked_reason, job.name), None)

    # Ensure the job has a stable identity (legacy jobs created before this feature)
    if job.job_id is None:
        job.job_id = uuid.uuid4().hex
        if conn is not None:
            db.update_job(conn, job.name, job_id=job.job_id)

    if not source.is_dir():
        return _finish(job, conn, now, "failed",
                       "source missing: %s" % source, None)
    if not dest_base.is_dir():
        return _finish(job, conn, now, "failed",
                       "destination missing: %s" % dest_base, None)

    if not force:
        ok, reason = integrity.verify(job)
        if not ok:
            if conn is not None:
                db.update_job(conn, job.name, blocked_reason=reason)
            return _finish(job, conn, now, "blocked",
                           "verification failed: %s" % reason, None)

    snaps_dir = _snapshots_dir(job)
    snaps_dir.mkdir(parents=True, exist_ok=True)

    stamp = now.strftime(TIMESTAMP_FMT)
    final = snaps_dir / stamp
    partial = snaps_dir / ("%s.partial" % stamp)
    if partial.is_dir() or partial.is_symlink():
        shutil.rmtree(partial, ignore_errors=True)

    previous = list_snapshots(job)
    cmd = ["rsync", "-a", "--delete", *IGNORE_FILTER]
    if previous:
        cmd.append("--link-dest=%s" % previous[-1])
    cmd.append("%s/" % source)
    cmd.append("%s/" % partial)

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode not in _RSYNC_OK:
        shutil.rmtree(partial, ignore_errors=True)
        msg = "rsync failed (code %d): %s" % (
            result.returncode, result.stderr.strip())
        return _finish(job, conn, now, "failed", msg, None)

    new_count, new_bytes = tree_stats(partial)
    if previous and not force:
        prev = previous[-1]
        prev_count = None
        if conn is not None:
            stats = db.get_snapshot_stats(conn, job.name, prev.name)
            if stats is not None:
                prev_count = stats[0]
        if prev_count is None:
            prev_count, _ = tree_stats(prev)
        # Refuse to rotate good snapshots away in favor of a suspiciously
        # shrunken copy: the classic cause is a source on a mount point whose
        # drive is absent — the directory exists but is empty.
        if prev_count and (new_count == 0 or (
                prev_count >= _SHRINK_MIN_FILES
                and new_count < prev_count * _SHRINK_RATIO)):
            shutil.rmtree(partial, ignore_errors=True)
            reason = ("source shrank from %d to %d files (unmounted drive, "
                      "emptied directory, or over-broad .backupignore?)"
                      % (prev_count, new_count))
            if conn is not None:
                db.update_job(conn, job.name, blocked_reason=reason)
            return _finish(
                job, conn, now, "blocked",
                "refusing to rotate snapshots: %s; run 'backup run %s "
                "--force' if this is intentional" % (reason, job.name), None)

    if final.is_dir():
        shutil.rmtree(final, ignore_errors=True)
    partial.replace(final)
    _update_latest(job, final)
    _prune(job, conn)

    try:
        integrity.write_marker(job, stamp)
    except OSError as exc:
        _log(job, "warning: could not write integrity marker: %s" % exc)
    if conn is not None:
        db.update_job(conn, job.name, last_snapshot=stamp, blocked_reason=None)
        db.record_snapshot(conn, job.name, stamp,
                           now.isoformat(timespec="seconds"),
                           file_count=new_count, total_bytes=new_bytes)

    suffix = " (forced, re-baselined)" if force else ""
    msg = "snapshot %s (%d kept)%s" % (stamp, len(list_snapshots(job)), suffix)
    return _finish(job, conn, now, "ok", msg, str(final))


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
        cmd = ["rsync", "-an", *IGNORE_FILTER, "--out-format=%n",
               "%s/" % source, "%s/" % tmp]
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return []
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


def _update_latest(job: db.Job, snapshot: Path) -> None:
    link = job_dir(job) / "latest"
    if link.is_symlink() or link.exists():
        try:
            link.unlink()
        except OSError as exc:
            _log(job, "warning: could not remove old latest symlink: %s" % exc)
    try:
        link.symlink_to(Path("snapshots") / snapshot.name)
    except OSError as exc:
        _log(job, "warning: could not update latest symlink: %s" % exc)


def _finish(job, conn, now, status, message, snapshot) -> RunResult:
    _log(job, "%s: %s" % (status, message))
    if conn is not None:
        db.record_run(conn, job.name, status, message,
                      now.isoformat(timespec="seconds"))
    return RunResult(status=status, message=message, snapshot=snapshot)
