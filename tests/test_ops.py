from __future__ import annotations

import shutil
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import backup.units as units
from backup import ops
from backup.db import (
    Job,
    add_job,
    connect,
    forget_snapshot,
    get_job,
    list_snapshot_names,
    record_snapshot,
    remove_job,
    update_job,
)
from backup.runner import job_dir, list_snapshots, run_backup

needs_rsync = pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")


class _CP:
    returncode = 0
    stdout = ""
    stderr = ""


def _silence_systemd(monkeypatch):
    monkeypatch.setattr(units, "_systemctl", lambda *a: _CP())
    monkeypatch.setattr(units, "is_active", lambda name: True)
    monkeypatch.setattr(units, "next_run", lambda name: None)


def make_job(tmp_path, name="docs", dest="dst", keep=7, mkdirs=True):
    src = tmp_path / "src"
    dst = tmp_path / dest
    if mkdirs:
        src.mkdir(exist_ok=True)
        dst.mkdir(exist_ok=True)
        (src / "a.txt").write_text("hello")
    return Job(
        name=name, source=str(src), dest=str(dst),
        oncalendar="*-*-* 02:00:00", schedule_human="daily at 02:00",
        keep=keep, created_at="2026-08-09T00:00:00", job_id="id-%s" % name,
    )


# ---------- db: snapshots table ----------

def test_record_and_list_snapshots(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, make_job(tmp_path, mkdirs=False))
    record_snapshot(conn, "docs", "2026-08-09_02-00-00")
    record_snapshot(conn, "docs", "2026-08-08_02-00-00")
    assert list_snapshot_names(conn, "docs") == [
        "2026-08-08_02-00-00", "2026-08-09_02-00-00"]


def test_record_snapshot_idempotent(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    record_snapshot(conn, "docs", "2026-08-09_02-00-00")
    record_snapshot(conn, "docs", "2026-08-09_02-00-00")  # no error
    assert list_snapshot_names(conn, "docs") == ["2026-08-09_02-00-00"]


def test_forget_snapshot(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    record_snapshot(conn, "docs", "s1")
    forget_snapshot(conn, "docs", "s1")
    assert list_snapshot_names(conn, "docs") == []


def test_remove_job_cascades_snapshots(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, make_job(tmp_path, mkdirs=False))
    record_snapshot(conn, "docs", "s1")
    remove_job(conn, "docs")
    assert list_snapshot_names(conn, "docs") == []


def test_rename_job_cascades_snapshots(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, make_job(tmp_path, mkdirs=False))
    record_snapshot(conn, "docs", "s1")
    update_job(conn, "docs", name="documents")
    assert list_snapshot_names(conn, "documents") == ["s1"]
    assert list_snapshot_names(conn, "docs") == []


def test_archived_columns_default_and_update(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, make_job(tmp_path, mkdirs=False))
    job = get_job(conn, "docs")
    assert job.archived_at is None
    assert job.archived_reason is None
    update_job(conn, "docs", archived_at="2026-08-09T12:00:00",
               archived_reason="manual")
    job = get_job(conn, "docs")
    assert job.archived_at == "2026-08-09T12:00:00"
    assert job.archived_reason == "manual"


# ---------- runner: snapshot recording ----------

@needs_rsync
def test_run_records_snapshot_row(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    job = make_job(tmp_path)
    add_job(conn, job)
    run_backup(job, conn=conn, now=datetime(2026, 8, 9, 2, 0, 0))
    assert list_snapshot_names(conn, "docs") == ["2026-08-09_02-00-00"]


@needs_rsync
def test_prune_forgets_pruned_rows(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    job = make_job(tmp_path, keep=2)
    add_job(conn, job)
    base = datetime(2026, 8, 9, 0, 0, 0)
    for i in range(4):
        run_backup(job, conn=conn, now=base + timedelta(hours=i))
    names = list_snapshot_names(conn, "docs")
    assert len(names) == 2
    assert names == [p.name for p in list_snapshots(job)]


def test_archived_job_refuses_to_run(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    job = make_job(tmp_path)
    job.archived_at = "2026-08-09T12:00:00"
    add_job(conn, job)
    res = run_backup(job, conn=conn, now=datetime(2026, 8, 9, 2, 0, 0))
    assert res.status == "archived"
    assert list_snapshots(job) == []
    # the refusal is not a run: last_run/last_status keep the pre-archive value
    assert get_job(conn, "docs").last_run_at is None
    assert get_job(conn, "docs").last_status is None


@needs_rsync
def test_prune_keeps_row_when_delete_fails(tmp_path, monkeypatch):
    import backup.runner as runner_mod
    conn = connect(tmp_path / "jobs.db")
    job = make_job(tmp_path, keep=1)
    add_job(conn, job)
    run_backup(job, conn=conn, now=datetime(2026, 8, 9, 0, 0, 0))
    # second run prunes the first, but deletion fails (e.g. read-only dest)
    real_rmtree = runner_mod.shutil.rmtree
    monkeypatch.setattr(runner_mod.shutil, "rmtree",
                        lambda *a, **k: None)  # rmtree silently does nothing
    run_backup(job, conn=conn, now=datetime(2026, 8, 9, 1, 0, 0))
    monkeypatch.setattr(runner_mod.shutil, "rmtree", real_rmtree)
    # the undeleted snapshot must still be tracked, not silently forgotten
    assert list_snapshot_names(conn, "docs") == [
        "2026-08-09_00-00-00", "2026-08-09_01-00-00"]


# ---------- runner: shrink guard (empty/unmounted source protection) ----------

@needs_rsync
def test_emptied_source_blocks_instead_of_rotating(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    job = make_job(tmp_path, keep=2)
    add_job(conn, job)
    r1 = run_backup(job, conn=conn, now=datetime(2026, 8, 9, 0, 0, 0))
    assert r1.status == "ok"
    (Path(job.source) / "a.txt").unlink()      # source now empty (mount gone?)
    r2 = run_backup(job, conn=conn, now=datetime(2026, 8, 9, 1, 0, 0))
    assert r2.status == "blocked"
    assert "shrank" in r2.message
    snaps = list_snapshots(job)
    assert [p.name for p in snaps] == ["2026-08-09_00-00-00"]  # good one kept
    assert get_job(conn, "docs").blocked_reason is not None
    # blocked latches: next scheduled run refuses too
    r3 = run_backup(get_job(conn, "docs"), conn=conn,
                    now=datetime(2026, 8, 9, 2, 0, 0))
    assert r3.status == "blocked"


@needs_rsync
def test_emptied_source_force_overrides(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    job = make_job(tmp_path)
    add_job(conn, job)
    run_backup(job, conn=conn, now=datetime(2026, 8, 9, 0, 0, 0))
    (Path(job.source) / "a.txt").unlink()
    res = run_backup(get_job(conn, "docs"), conn=conn,
                     now=datetime(2026, 8, 9, 1, 0, 0), force=True)
    assert res.status == "ok"                  # explicit intent wins
    assert len(list_snapshots(job)) == 2


@needs_rsync
def test_drastic_shrink_blocks(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    job = make_job(tmp_path)
    add_job(conn, job)
    src = Path(job.source)
    for i in range(30):
        (src / ("f%02d.txt" % i)).write_text("x")
    run_backup(job, conn=conn, now=datetime(2026, 8, 9, 0, 0, 0))
    for i in range(30):                        # keep only a.txt + 1 file
        if i != 0:
            (src / ("f%02d.txt" % i)).unlink()
    res = run_backup(get_job(conn, "docs"), conn=conn,
                     now=datetime(2026, 8, 9, 1, 0, 0))
    assert res.status == "blocked"


@needs_rsync
def test_small_tree_edits_do_not_block(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    job = make_job(tmp_path)                   # 1 file
    add_job(conn, job)
    (Path(job.source) / "b.txt").write_text("y")
    run_backup(job, conn=conn, now=datetime(2026, 8, 9, 0, 0, 0))
    (Path(job.source) / "b.txt").unlink()      # 2 -> 1 files: normal editing
    res = run_backup(get_job(conn, "docs"), conn=conn,
                     now=datetime(2026, 8, 9, 1, 0, 0))
    assert res.status == "ok"


@needs_rsync
def test_run_records_snapshot_fingerprint(tmp_path):
    import backup.db as dbmod
    conn = connect(tmp_path / "jobs.db")
    job = make_job(tmp_path)
    add_job(conn, job)
    run_backup(job, conn=conn, now=datetime(2026, 8, 9, 2, 0, 0))
    stats = dbmod.get_snapshot_stats(conn, "docs", "2026-08-09_02-00-00")
    assert stats == (1, len("hello"))


# ---------- ops: snapshot verification ----------

def test_verify_snapshot_lifecycle(tmp_path):
    import backup.db as dbmod
    conn = connect(tmp_path / "jobs.db")
    job = make_job(tmp_path)
    add_job(conn, job)
    snap = _fake_snapshot(job, "2026-08-08_02-00-00")

    verdict, _ = ops.verify_snapshot(conn, job, "2026-08-08_02-00-00")
    assert verdict == "baseline"               # first check adopts a fingerprint
    verdict, _ = ops.verify_snapshot(conn, job, "2026-08-08_02-00-00")
    assert verdict == "ok"                     # unchanged since

    (snap / "a.txt").unlink()                  # corruption / partial delete
    verdict, msg = ops.verify_snapshot(conn, job, "2026-08-08_02-00-00")
    assert verdict == "suspect"
    assert "found 0 files" in msg

    dbmod.record_snapshot(conn, "docs", "2026-01-01_00-00-00")
    verdict, _ = ops.verify_snapshot(conn, job, "2026-01-01_00-00-00")
    assert verdict == "lost"


# ---------- ops: restore overwrite counting ----------

def test_count_overwrites(tmp_path):
    job = make_job(tmp_path)
    snap = job_dir(job) / "snapshots" / "s1"
    (snap / "sub").mkdir(parents=True)
    (snap / "a.txt").write_text("x")
    (snap / "sub" / "b.txt").write_text("x")
    target = tmp_path / "t"
    assert ops.count_overwrites(job, "s1", target) == 0      # target missing
    (target / "sub").mkdir(parents=True)
    (target / "a.txt").write_text("current")
    assert ops.count_overwrites(job, "s1", target) == 1      # only a.txt clashes
    (target / "sub" / "b.txt").write_text("current")
    assert ops.count_overwrites(job, "s1", target) == 2


# ---------- ops: purge identity check ----------

def test_purge_refuses_foreign_marker(tmp_path, monkeypatch):
    from backup import integrity
    _silence_systemd(monkeypatch)
    monkeypatch.setattr(units, "remove_units", lambda name: None)
    conn = connect(tmp_path / "jobs.db")
    job = make_job(tmp_path)
    add_job(conn, job)
    snap = _fake_snapshot(job, "2026-08-08_02-00-00")
    integrity.write_marker(job, "2026-08-08_02-00-00")

    foreign = make_job(tmp_path, mkdirs=False)  # same name/dest, other identity
    foreign.job_id = "someone-else"
    with pytest.raises(ValueError):
        ops.delete_job(conn, foreign, purge=True)
    assert snap.is_dir()                        # nothing was deleted
    assert get_job(conn, "docs") is not None    # job record intact too

    ops.delete_job(conn, job, purge=True)       # matching identity purges fine
    assert not job_dir(job).exists()


# ---------- ops: task grouping ----------

def test_group_tasks_by_source(tmp_path):
    j1 = make_job(tmp_path, name="docs", dest="d1", mkdirs=False)
    j2 = make_job(tmp_path, name="docs-usb", dest="d2", mkdirs=False)
    j3 = Job(name="other", source=str(tmp_path / "other"), dest=str(tmp_path / "d1"),
             oncalendar="x", schedule_human="x", keep=7, created_at="t")
    tasks = ops.group_tasks([j1, j2, j3])
    assert len(tasks) == 2
    by_src = {t.source: t for t in tasks}
    assert [j.name for j in by_src[j1.source].jobs] == ["docs", "docs-usb"]
    assert [j.name for j in by_src[j3.source].jobs] == ["other"]


def test_task_archived_only_when_all_jobs_archived(tmp_path):
    j1 = make_job(tmp_path, name="docs", dest="d1", mkdirs=False)
    j2 = make_job(tmp_path, name="docs-usb", dest="d2", mkdirs=False)
    j1.archived_at = "2026-08-09T12:00:00"
    task = ops.group_tasks([j1, j2])[0]
    assert task.archived is False
    j2.archived_at = "2026-08-09T12:00:00"
    task = ops.group_tasks([j1, j2])[0]
    assert task.archived is True


# ---------- ops: snapshot status (lost detection) ----------

def _fake_snapshot(job, name):
    d = job_dir(job) / "snapshots" / name
    d.mkdir(parents=True)
    (d / "a.txt").write_text("x")
    return d


def test_snapshot_status_marks_lost(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    job = make_job(tmp_path)
    add_job(conn, job)
    _fake_snapshot(job, "2026-08-08_02-00-00")
    record_snapshot(conn, "docs", "2026-08-08_02-00-00")
    record_snapshot(conn, "docs", "2026-08-09_02-00-00")  # not on disk -> lost
    status = ops.snapshot_status(conn, job)
    by_name = {s.name: s for s in status}
    assert by_name["2026-08-08_02-00-00"].lost is False
    assert by_name["2026-08-09_02-00-00"].lost is True
    # newest first
    assert [s.name for s in status] == [
        "2026-08-09_02-00-00", "2026-08-08_02-00-00"]


def test_snapshot_status_backfills_untracked_disk_snapshots(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    job = make_job(tmp_path)
    add_job(conn, job)
    _fake_snapshot(job, "2026-08-07_02-00-00")  # pre-feature snapshot, not in db
    status = ops.snapshot_status(conn, job)
    assert [s.name for s in status] == ["2026-08-07_02-00-00"]
    assert status[0].lost is False
    assert list_snapshot_names(conn, "docs") == ["2026-08-07_02-00-00"]


# ---------- ops: archive / unarchive / delete ----------

def test_archive_job_stamps_and_removes_units(tmp_path, monkeypatch):
    _silence_systemd(monkeypatch)
    removed = []
    monkeypatch.setattr(units, "remove_units", lambda name: removed.append(name))
    conn = connect(tmp_path / "jobs.db")
    job = make_job(tmp_path)
    add_job(conn, job)
    ops.archive_job(conn, job, reason="manual")
    assert removed == ["docs"]
    job = get_job(conn, "docs")
    assert job.archived_at is not None
    assert job.archived_reason == "manual"


def test_unarchive_job_reinstalls_units(tmp_path, monkeypatch):
    _silence_systemd(monkeypatch)
    installed = []
    monkeypatch.setattr(units, "remove_units", lambda name: None)
    monkeypatch.setattr(
        units, "install_units",
        lambda name, oncal, exe, src: installed.append(name))
    conn = connect(tmp_path / "jobs.db")
    job = make_job(tmp_path)
    add_job(conn, job)
    ops.archive_job(conn, job, reason="manual")
    ops.unarchive_job(conn, get_job(conn, "docs"))
    assert installed == ["docs"]
    job = get_job(conn, "docs")
    assert job.archived_at is None
    assert job.archived_reason is None


def test_unarchive_refuses_missing_source(tmp_path, monkeypatch):
    _silence_systemd(monkeypatch)
    monkeypatch.setattr(units, "remove_units", lambda name: None)
    conn = connect(tmp_path / "jobs.db")
    job = make_job(tmp_path, mkdirs=False)  # source dir never created
    add_job(conn, job)
    ops.archive_job(conn, job, reason="source missing")
    with pytest.raises(ValueError):
        ops.unarchive_job(conn, get_job(conn, "docs"))


def test_auto_archive_missing_sources(tmp_path, monkeypatch):
    _silence_systemd(monkeypatch)
    # Auto-archive may fire on a transiently missing source (unmounted drive):
    # it must only disable the timer, never delete the unit files.
    removed = []
    monkeypatch.setattr(units, "remove_units", lambda name: removed.append(name))
    conn = connect(tmp_path / "jobs.db")
    alive = make_job(tmp_path)               # source exists
    gone = make_job(tmp_path, name="gone", dest="d2", mkdirs=False)
    gone.source = str(tmp_path / "nonexistent")
    add_job(conn, alive)
    add_job(conn, gone)
    archived = ops.auto_archive_missing_sources(conn, [alive, gone])
    assert archived == ["gone"]
    assert get_job(conn, "docs").archived_at is None
    job = get_job(conn, "gone")
    assert job.archived_at is not None
    assert job.archived_reason == "source missing"
    assert removed == []                     # units disabled, not deleted
    # idempotent: already-archived jobs are not re-archived
    jobs = [get_job(conn, "docs"), get_job(conn, "gone")]
    assert ops.auto_archive_missing_sources(conn, jobs) == []


def test_delete_job_keeps_snapshots_by_default(tmp_path, monkeypatch):
    _silence_systemd(monkeypatch)
    monkeypatch.setattr(units, "remove_units", lambda name: None)
    conn = connect(tmp_path / "jobs.db")
    job = make_job(tmp_path)
    add_job(conn, job)
    snap = _fake_snapshot(job, "2026-08-08_02-00-00")
    ops.delete_job(conn, job)
    assert get_job(conn, "docs") is None
    assert snap.is_dir()


def test_delete_job_purge_removes_snapshots(tmp_path, monkeypatch):
    _silence_systemd(monkeypatch)
    monkeypatch.setattr(units, "remove_units", lambda name: None)
    conn = connect(tmp_path / "jobs.db")
    job = make_job(tmp_path)
    add_job(conn, job)
    _fake_snapshot(job, "2026-08-08_02-00-00")
    ops.delete_job(conn, job, purge=True)
    assert get_job(conn, "docs") is None
    assert not job_dir(job).exists()


def test_archive_lost_detection(tmp_path):
    job = make_job(tmp_path)
    assert ops.archive_lost(job) is True         # no snapshots anywhere
    _fake_snapshot(job, "2026-08-08_02-00-00")
    assert ops.archive_lost(job) is False


# ---------- ops: restore ----------

def test_default_restore_target_prefers_source(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    job = make_job(tmp_path)
    assert ops.default_restore_target(job) == Path(job.source)


def test_default_restore_target_falls_back_to_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    job = make_job(tmp_path, mkdirs=False)
    job.source = str(tmp_path / "gone-project")
    assert ops.default_restore_target(job) == tmp_path / "home" / "gone-project"


@needs_rsync
def test_restore_snapshot_copies_files(tmp_path):
    job = make_job(tmp_path)
    _fake_snapshot(job, "2026-08-08_02-00-00")
    target = tmp_path / "recovered"
    ok, msg = ops.restore_snapshot(job, "2026-08-08_02-00-00", target)
    assert ok, msg
    assert (target / "a.txt").read_text() == "x"


def test_restore_snapshot_refuses_missing(tmp_path):
    job = make_job(tmp_path)
    ok, msg = ops.restore_snapshot(job, "2026-01-01_00-00-00", tmp_path / "out")
    assert not ok
    assert "not found" in msg.lower() or "lost" in msg.lower()
