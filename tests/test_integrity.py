from __future__ import annotations

from pathlib import Path

from backup import integrity
from backup.db import Job


def make_job(tmp_path, name="proj", job_id="id-1", last_snapshot=None):
    dest = tmp_path / "dst"
    dest.mkdir(exist_ok=True)
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    return Job(
        name=name, source=str(src), dest=str(dest), oncalendar="hourly",
        schedule_human="every hour", keep=7, created_at="2026-06-29T00:00:00",
        job_id=job_id, last_snapshot=last_snapshot,
    )


def _make_snapshot(job, stamp):
    d = Path(job.dest) / job.name / "snapshots" / stamp
    d.mkdir(parents=True, exist_ok=True)


def test_marker_roundtrip(tmp_path):
    job = make_job(tmp_path)
    assert integrity.read_marker(job) is None
    integrity.write_marker(job, "2026-06-29_01-00-00")
    data = integrity.read_marker(job)
    assert data["job_id"] == "id-1"
    assert data["source"] == job.source
    assert data["last_snapshot"] == "2026-06-29_01-00-00"


def test_corrupt_marker_reads_as_none(tmp_path):
    job = make_job(tmp_path)
    p = integrity.marker_path(job)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ not json")
    assert integrity.read_marker(job) is None


def test_verify_first_run_ok(tmp_path):
    job = make_job(tmp_path, last_snapshot=None)
    assert integrity.verify(job) == (True, None)


def test_verify_first_run_foreign_marker_blocks(tmp_path):
    job = make_job(tmp_path, job_id="id-1", last_snapshot=None)
    other = make_job(tmp_path, job_id="id-OTHER")
    integrity.write_marker(other, None)  # someone else's marker already there
    ok, reason = integrity.verify(job)
    assert ok is False
    assert "id-OTHER" in reason


def test_verify_match_ok(tmp_path):
    job = make_job(tmp_path, last_snapshot="2026-06-29_01-00-00")
    _make_snapshot(job, "2026-06-29_01-00-00")
    integrity.write_marker(job, "2026-06-29_01-00-00")
    assert integrity.verify(job) == (True, None)


def test_verify_marker_missing_blocks(tmp_path):
    job = make_job(tmp_path, last_snapshot="2026-06-29_01-00-00")
    _make_snapshot(job, "2026-06-29_01-00-00")
    ok, reason = integrity.verify(job)
    assert ok is False
    assert "marker missing" in reason


def test_verify_id_mismatch_blocks(tmp_path):
    job = make_job(tmp_path, job_id="id-1", last_snapshot="2026-06-29_01-00-00")
    _make_snapshot(job, "2026-06-29_01-00-00")
    integrity.write_marker(job, "2026-06-29_01-00-00")
    job.job_id = "id-CHANGED"
    ok, reason = integrity.verify(job)
    assert ok is False
    assert "different job" in reason


def test_verify_source_changed_blocks(tmp_path):
    job = make_job(tmp_path, last_snapshot="2026-06-29_01-00-00")
    _make_snapshot(job, "2026-06-29_01-00-00")
    integrity.write_marker(job, "2026-06-29_01-00-00")
    job.source = "/somewhere/else"
    ok, reason = integrity.verify(job)
    assert ok is False
    assert "source" in reason


def test_verify_missing_snapshot_blocks(tmp_path):
    job = make_job(tmp_path, last_snapshot="2026-06-29_01-00-00")
    integrity.write_marker(job, "2026-06-29_01-00-00")  # marker says so, but dir absent
    ok, reason = integrity.verify(job)
    assert ok is False
    assert "missing from destination" in reason


def test_verify_marker_out_of_sync_blocks(tmp_path):
    job = make_job(tmp_path, last_snapshot="2026-06-29_02-00-00")
    _make_snapshot(job, "2026-06-29_02-00-00")
    integrity.write_marker(job, "2026-06-29_01-00-00")  # marker lags DB
    ok, reason = integrity.verify(job)
    assert ok is False
    assert "out of sync" in reason
