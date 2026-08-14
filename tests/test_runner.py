from __future__ import annotations

import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from backup.db import Job
from backup.runner import job_dir, list_snapshots, run_backup, preview_backup


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
    (Path(job.source) / "b.txt").write_text("new")  # a.txt stays untouched
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
        # distinct sizes: rsync's quick-check (size + whole-second mtime)
        # cannot see same-second same-size edits, with or without this feature
        (Path(job.source) / "a.txt").write_text("v" * (i + 1))
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
    (Path(job.source) / "a.txt").write_text("v2")
    run_backup(job, now=datetime(2026, 6, 28, 3, 0, 0))
    snaps = list_snapshots(job)
    assert len(snaps) == 2
    assert (job_dir(job) / "latest").resolve() == snaps[-1].resolve()


def test_missing_source_fails(tmp_path):
    job = make_job(tmp_path)
    shutil.rmtree(job.source)
    res = run_backup(job, now=datetime(2026, 6, 28, 2, 0, 0))
    assert res.status == "failed"


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


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_legacy_job_adopts_existing_destination_without_block(tmp_path):
    job = make_job(tmp_path)  # job_id None, last_snapshot None (pre-feature job)
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, job)
    # a snapshot already exists on the destination from an older version
    old_snap = job_dir(job) / "snapshots" / "2026-06-01_00-00-00"
    old_snap.mkdir(parents=True)
    (old_snap / "old.txt").write_text("old")
    res = run_backup(job, conn=conn, now=datetime(2026, 6, 28, 2, 0, 0))
    assert res.status == "ok"                       # adopted, not blocked
    assert get_job(conn, job.name).job_id is not None  # id assigned + persisted
    assert old_snap.is_dir()                        # pre-existing snapshot preserved
    assert (old_snap / "old.txt").read_text() == "old"


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_two_runs_in_same_second_do_not_crash(tmp_path):
    job = make_job(tmp_path)
    job.job_id = "id-1"
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, job)
    t = datetime(2026, 6, 28, 2, 0, 0)
    r1 = run_backup(job, conn=conn, now=t)
    assert r1.status == "ok"
    (Path(job.source) / "a.txt").write_text("changed")
    reloaded = get_job(conn, job.name)
    r2 = run_backup(reloaded, conn=conn, now=t)  # same timestamp
    assert r2.status == "ok"                      # must not crash
    assert len(list_snapshots(job)) == 1          # same-second snapshot superseded, not duplicated


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_backupignore_excludes_files_nested(tmp_path):
    job = make_job(tmp_path)
    job.job_id = "id-1"
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, job)
    src = Path(job.source)
    (src / "keep.txt").write_text("k")
    (src / "secret.log").write_text("s")
    (src / "sub").mkdir()
    (src / "sub" / "keep2.txt").write_text("k2")
    (src / "sub" / "tmp.cache").write_text("c")
    (src / ".backupignore").write_text("*.log\n")
    (src / "sub" / ".backupignore").write_text("*.cache\n")
    res = run_backup(job, conn=conn, now=datetime(2026, 6, 28, 2, 0, 0))
    assert res.status == "ok"
    snap = list_snapshots(job)[-1]
    assert (snap / "keep.txt").exists()
    assert (snap / "sub" / "keep2.txt").exists()
    assert not (snap / "secret.log").exists()       # top-level *.log ignored
    assert not (snap / "sub" / "tmp.cache").exists() # nested *.cache ignored


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_preview_lists_included_excludes_ignored_and_writes_nothing(tmp_path):
    job = make_job(tmp_path)
    src = Path(job.source)
    (src / "keep.txt").write_text("k")
    (src / "secret.log").write_text("s")
    (src / ".backupignore").write_text("*.log\n")
    files = preview_backup(job)
    assert "keep.txt" in files
    assert "secret.log" not in files
    assert list_snapshots(job) == []  # preview created no snapshot


def test_preview_missing_source_returns_empty(tmp_path):
    job = make_job(tmp_path)
    shutil.rmtree(job.source)
    assert preview_backup(job) == []


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_preview_includes_symlinks(tmp_path):
    job = make_job(tmp_path)
    src = Path(job.source)
    (src / "real.txt").write_text("r")
    (src / "alink.txt").symlink_to(src / "real.txt")
    files = preview_backup(job)
    assert "real.txt" in files
    assert "alink.txt" in files  # archive mode preserves symlinks; preview must show them


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_unchanged_source_skips_snapshot(tmp_path):
    job = make_job(tmp_path)
    job.job_id = "id-1"
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, job)
    run_backup(job, conn=conn, now=datetime(2026, 6, 28, 2, 0, 0))
    reloaded = get_job(conn, job.name)
    res = run_backup(reloaded, conn=conn, now=datetime(2026, 6, 29, 2, 0, 0))
    assert res.status == "unchanged"
    assert res.snapshot is None
    assert len(list_snapshots(job)) == 1  # nothing rotated in
    after = get_job(conn, job.name)
    assert after.last_snapshot == "2026-06-28_02-00-00"  # untouched
    assert after.last_status == "unchanged"              # run still recorded
    assert after.last_run_at == "2026-06-29T02:00:00"


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_change_after_skipped_run_creates_snapshot(tmp_path):
    job = make_job(tmp_path)
    run_backup(job, now=datetime(2026, 6, 28, 2, 0, 0))
    run_backup(job, now=datetime(2026, 6, 29, 2, 0, 0))  # unchanged, skipped
    (Path(job.source) / "a.txt").write_text("edited")
    res = run_backup(job, now=datetime(2026, 6, 30, 2, 0, 0))
    assert res.status == "ok"
    snaps = list_snapshots(job)
    assert len(snaps) == 2
    assert (snaps[-1] / "a.txt").read_text() == "edited"


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_deletion_counts_as_change(tmp_path):
    job = make_job(tmp_path)
    (Path(job.source) / "b.txt").write_text("second")
    run_backup(job, now=datetime(2026, 6, 28, 2, 0, 0))
    (Path(job.source) / "b.txt").unlink()
    res = run_backup(job, now=datetime(2026, 6, 29, 2, 0, 0))
    assert res.status == "ok"
    snaps = list_snapshots(job)
    assert len(snaps) == 2
    assert not (snaps[-1] / "b.txt").exists()


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_force_snapshots_even_when_unchanged(tmp_path):
    job = make_job(tmp_path)
    run_backup(job, now=datetime(2026, 6, 28, 2, 0, 0))
    res = run_backup(job, now=datetime(2026, 6, 29, 2, 0, 0), force=True)
    assert res.status == "ok"
    assert len(list_snapshots(job)) == 2


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_rsync_omits_symlink_times(tmp_path, monkeypatch):
    """Destinations without lutimes() (sshfs, some network mounts) make rsync
    follow a symlink to set its times; when the target is outside the backed-up
    tree the link dangles there and rsync fails the whole run with code 23."""
    import subprocess as _sp

    from backup import runner as _runner

    job = make_job(tmp_path)
    calls = []
    real_run = _sp.run

    def spy(cmd, **kwargs):
        calls.append(list(cmd))
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(_runner.subprocess, "run", spy)
    res = run_backup(job, now=datetime(2026, 6, 28, 2, 0, 0))
    assert res.status == "ok"
    transfers = [c for c in calls if c[0] == "rsync" and "-n" not in c]
    assert transfers, "no rsync transfer command was issued"
    for cmd in transfers:
        assert "--omit-link-times" in cmd


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_snapshot_keeps_symlink_pointing_outside_the_source(tmp_path):
    job = make_job(tmp_path)
    src = Path(job.source)
    (src / "sub").mkdir()
    (src / "sub" / "gene.npz").symlink_to("../../elsewhere/gene.npz")
    res = run_backup(job, now=datetime(2026, 6, 28, 2, 0, 0))
    assert res.status == "ok"
    link = list_snapshots(job)[0] / "sub" / "gene.npz"
    assert link.is_symlink()
    assert os.readlink(link) == "../../elsewhere/gene.npz"


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_symlink_only_tree_is_unchanged_on_the_second_run(tmp_path):
    """The change check must ignore exactly what the transfer omits: link
    times are never synced, so they must not read as a change forever."""
    job = make_job(tmp_path)
    (Path(job.source) / "gene.npz").symlink_to("../elsewhere/gene.npz")
    run_backup(job, now=datetime(2026, 6, 28, 2, 0, 0))
    res = run_backup(job, now=datetime(2026, 6, 29, 2, 0, 0))
    assert res.status == "unchanged"
