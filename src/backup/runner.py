from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from . import db, paths

TIMESTAMP_FMT = "%Y-%m-%d_%H-%M-%S"
_RSYNC_OK = {0, 24}  # 24 = some source files vanished during transfer


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


def _log(job: db.Job, message: str) -> None:
    try:
        paths.log_dir().mkdir(parents=True, exist_ok=True)
        logfile = paths.log_dir() / ("%s.log" % job.name)
        with logfile.open("a") as fh:
            fh.write("%s %s\n" % (datetime.now().isoformat(timespec="seconds"), message))
    except OSError:
        pass  # logging must never crash a backup run


def _prune(job: db.Job) -> None:
    snaps = list_snapshots(job)
    excess = len(snaps) - job.keep
    for old in snaps[:max(0, excess)]:
        shutil.rmtree(old, ignore_errors=True)


def run_backup(
    job: db.Job, conn=None, now: Optional[datetime] = None
) -> RunResult:
    now = now or datetime.now()
    source = Path(job.source)
    dest_base = Path(job.dest)

    if not source.is_dir():
        return _finish(job, conn, now, "failed",
                       "source missing: %s" % source, None)
    if not dest_base.is_dir():
        return _finish(job, conn, now, "failed",
                       "destination missing: %s" % dest_base, None)

    snaps_dir = _snapshots_dir(job)
    snaps_dir.mkdir(parents=True, exist_ok=True)

    stamp = now.strftime(TIMESTAMP_FMT)
    final = snaps_dir / stamp
    partial = snaps_dir / ("%s.partial" % stamp)
    if partial.is_dir() or partial.is_symlink():
        shutil.rmtree(partial, ignore_errors=True)

    previous = list_snapshots(job)
    cmd = ["rsync", "-a", "--delete"]
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

    partial.replace(final)
    _update_latest(job, final)
    _prune(job)

    msg = "snapshot %s (%d kept)" % (stamp, len(list_snapshots(job)))
    return _finish(job, conn, now, "ok", msg, str(final))


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
