from __future__ import annotations

import os
import shutil
from datetime import datetime, timedelta

import pytest

from backup.db import Job
from backup.runner import job_dir, list_snapshots, run_backup


def make_job(tmp_path, keep=7):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "a.txt").write_text("hello")
    return Job(
        name="docs", source=str(src), dest=str(dst),
        oncalendar="x", schedule_human="x", keep=keep,
        created_at="2026-06-28T00:00:00",
    )


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_snapshot_copies_files(tmp_path):
    job = make_job(tmp_path)
    res = run_backup(job, now=datetime(2026, 6, 28, 2, 0, 0))
    assert res.status == "ok"
    snaps = list_snapshots(job)
    assert len(snaps) == 1
    assert (snaps[0] / "a.txt").read_text() == "hello"
    assert (job_dir(job) / "latest").resolve() == snaps[0].resolve()


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_unchanged_files_are_hardlinked(tmp_path):
    job = make_job(tmp_path)
    run_backup(job, now=datetime(2026, 6, 28, 2, 0, 0))
    run_backup(job, now=datetime(2026, 6, 28, 3, 0, 0))
    snaps = list_snapshots(job)
    assert len(snaps) == 2
    ino0 = os.stat(snaps[0] / "a.txt").st_ino
    ino1 = os.stat(snaps[1] / "a.txt").st_ino
    assert ino0 == ino1  # hard-linked, no extra space


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_retention_prunes_oldest(tmp_path):
    job = make_job(tmp_path, keep=2)
    base = datetime(2026, 6, 28, 0, 0, 0)
    for i in range(4):
        run_backup(job, now=base + timedelta(hours=i))
    snaps = list_snapshots(job)
    assert len(snaps) == 2  # only newest 2 kept


def test_missing_dest_fails(tmp_path):
    job = make_job(tmp_path)
    shutil.rmtree(job.dest)  # destination base gone
    res = run_backup(job, now=datetime(2026, 6, 28, 2, 0, 0))
    assert res.status == "failed"


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_latest_points_to_newest_after_multiple_runs(tmp_path):
    job = make_job(tmp_path)
    run_backup(job, now=datetime(2026, 6, 28, 2, 0, 0))
    run_backup(job, now=datetime(2026, 6, 28, 3, 0, 0))
    snaps = list_snapshots(job)
    assert (job_dir(job) / "latest").resolve() == snaps[-1].resolve()


def test_missing_source_fails(tmp_path):
    job = make_job(tmp_path)
    shutil.rmtree(job.source)
    res = run_backup(job, now=datetime(2026, 6, 28, 2, 0, 0))
    assert res.status == "failed"


import json as _json

from backup import integrity
from backup.db import connect, add_job, get_job


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_success_writes_marker_and_records_last_snapshot(tmp_path):
    job = make_job(tmp_path)
    job.job_id = "id-1"
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, job)
    res = run_backup(job, conn=conn, now=datetime(2026, 6, 28, 2, 0, 0))
    assert res.status == "ok"
    marker = integrity.read_marker(job)
    assert marker["job_id"] == "id-1"
    assert marker["last_snapshot"] == "2026-06-28_02-00-00"
    assert get_job(conn, job.name).last_snapshot == "2026-06-28_02-00-00"


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_mismatch_blocks_and_writes_no_snapshot(tmp_path):
    job = make_job(tmp_path)
    job.job_id = "id-1"
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, job)
    run_backup(job, conn=conn, now=datetime(2026, 6, 28, 2, 0, 0))  # baseline
    # Corrupt the destination: delete the marker so verify fails
    integrity.marker_path(job).unlink()
    reloaded = get_job(conn, job.name)
    res = run_backup(reloaded, conn=conn, now=datetime(2026, 6, 28, 3, 0, 0))
    assert res.status == "blocked"
    assert get_job(conn, job.name).blocked_reason is not None
    assert len(list_snapshots(job)) == 1  # no new snapshot created


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_blocked_latch_refuses_until_force(tmp_path):
    job = make_job(tmp_path)
    job.job_id = "id-1"
    job.blocked_reason = "previously blocked"
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, job)
    res = run_backup(job, conn=conn, now=datetime(2026, 6, 28, 2, 0, 0))
    assert res.status == "blocked"
    assert len(list_snapshots(job)) == 0  # nothing ran


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_force_rebaselines_and_clears_blocked(tmp_path):
    job = make_job(tmp_path)
    job.job_id = "id-1"
    job.blocked_reason = "previously blocked"
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, job)
    res = run_backup(job, conn=conn, now=datetime(2026, 6, 28, 2, 0, 0), force=True)
    assert res.status == "ok"
    assert get_job(conn, job.name).blocked_reason is None
    assert integrity.read_marker(job) is not None
