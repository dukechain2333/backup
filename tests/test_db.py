from __future__ import annotations

import pytest

from backup.db import (
    Job,
    add_job,
    connect,
    get_config,
    get_job,
    get_job_by_source,
    list_jobs,
    record_run,
    remove_job,
    set_config,
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


def test_get_config_missing_returns_default(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    assert get_config(conn, "default_dest") is None
    assert get_config(conn, "default_dest", "/fallback") == "/fallback"


def test_set_and_get_config(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    set_config(conn, "default_dest", "/mnt/backups")
    assert get_config(conn, "default_dest") == "/mnt/backups"


def test_set_config_upserts(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    set_config(conn, "default_dest", "/old")
    set_config(conn, "default_dest", "/new")
    assert get_config(conn, "default_dest") == "/new"


def test_column_exists_reports_presence(tmp_path):
    import backup.db as dbmod
    conn = connect(tmp_path / "jobs.db")
    assert dbmod._column_exists(conn, "jobs", "keep") is True
    assert dbmod._column_exists(conn, "jobs", "nope") is False


def test_ensure_column_adds_missing_and_is_idempotent(tmp_path):
    import backup.db as dbmod
    conn = connect(tmp_path / "jobs.db")
    add_job(conn, make_job())
    assert dbmod._column_exists(conn, "jobs", "priority") is False
    dbmod._ensure_column(conn, "jobs", "priority", "INTEGER")
    assert dbmod._column_exists(conn, "jobs", "priority") is True
    dbmod._ensure_column(conn, "jobs", "priority", "INTEGER")  # idempotent: no error
    assert get_job(conn, "docs").source == "/a/docs"  # existing data preserved


def test_connect_upgrades_old_db_with_added_columns(tmp_path, monkeypatch):
    import backup.db as dbmod
    path = tmp_path / "jobs.db"
    conn = connect(path)              # "old" version DB
    add_job(conn, make_job())
    conn.close()
    # Newer version declares an added column:
    monkeypatch.setattr(dbmod, "_ADDED_COLUMNS", [("jobs", "notes", "TEXT")])
    conn2 = connect(path)            # reopened by "new" code -> migrates in place
    assert dbmod._column_exists(conn2, "jobs", "notes") is True
    assert get_job(conn2, "docs").source == "/a/docs"  # data survived the upgrade
