from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from . import db

MARKER_NAME = ".backup-meta.json"


def marker_path(job: db.Job) -> Path:
    return Path(job.dest) / job.name / MARKER_NAME


def read_marker(job: db.Job) -> Optional[dict]:
    try:
        with marker_path(job).open() as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_marker(job: db.Job, last_snapshot: Optional[str]) -> None:
    path = marker_path(job)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "job_id": job.job_id,
        "name": job.name,
        "source": job.source,
        "last_snapshot": last_snapshot,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    tmp = path.with_name(MARKER_NAME + ".tmp")
    with tmp.open("w") as fh:
        json.dump(data, fh, indent=2)
    tmp.replace(path)


def verify(job: db.Job) -> Tuple[bool, Optional[str]]:
    marker = read_marker(job)
    has_baseline = job.last_snapshot is not None

    if not has_baseline:
        if marker is not None and marker.get("job_id") != job.job_id:
            return False, "destination already belongs to job %s" % marker.get("job_id")
        return True, None

    if marker is None:
        return False, "destination marker missing (dest moved, unmounted, or wiped?)"
    if marker.get("job_id") != job.job_id:
        return False, "destination belongs to a different job (id mismatch)"
    if marker.get("source") != job.source:
        return False, "source path changed since last backup"
    snap = Path(job.dest) / job.name / "snapshots" / job.last_snapshot
    if not snap.is_dir():
        return False, ("recorded last snapshot %s missing from destination "
                       "(content changed)" % job.last_snapshot)
    if marker.get("last_snapshot") != job.last_snapshot:
        return False, "destination marker out of sync with records"
    return True, None
