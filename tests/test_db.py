from __future__ import annotations

import pytest

from backup.db import (
    Job,
    add_job,
    connect,
    get_job,
    get_job_by_source,
    list_jobs,
    record_run,
    remove_job,
    update_job,
)


def make_job(name="docs", source="/a/docs", dest="/b"):
    return Job(
        name=name,
        source=source,
        dest=dest,
        oncalendar="*-*-* 02:00:00",
        schedule_human="daily at 02:00",
        keep=7,
        created_at="2026-06-28T00:00:00",
    )


def test_add_and_get(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, make_job())
    job = get_job(conn, "docs")
    assert job is not None
    assert job.source == "/a/docs"
    assert job.keep == 7


def test_duplicate_name_rejected(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, make_job())
    with pytest.raises(ValueError):
        add_job(conn, make_job(source="/a/other"))


def test_duplicate_source_rejected(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, make_job())
    with pytest.raises(ValueError):
        add_job(conn, make_job(name="other"))


def test_get_by_source(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, make_job())
    assert get_job_by_source(conn, "/a/docs").name == "docs"
    assert get_job_by_source(conn, "/nope") is None


def test_list_ordered(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, make_job(name="zeta", source="/z"))
    add_job(conn, make_job(name="alpha", source="/a"))
    assert [j.name for j in list_jobs(conn)] == ["alpha", "zeta"]


def test_update_and_rename(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, make_job())
    update_job(conn, "docs", keep=3, name="documents")
    assert get_job(conn, "docs") is None
    assert get_job(conn, "documents").keep == 3


def test_record_run(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, make_job())
    record_run(conn, "docs", "ok", "1 snapshot", "2026-06-28T02:00:00")
    job = get_job(conn, "docs")
    assert job.last_status == "ok"
    assert job.last_run_at == "2026-06-28T02:00:00"


def test_remove(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, make_job())
    assert remove_job(conn, "docs") is True
    assert remove_job(conn, "docs") is False
