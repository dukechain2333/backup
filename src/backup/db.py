from __future__ import annotations

import sqlite3
from dataclasses import dataclass, fields
from pathlib import Path
from typing import List, Optional

from . import paths

_COLUMNS = (
    "name", "source", "dest", "oncalendar", "schedule_human",
    "keep", "created_at", "last_run_at", "last_status", "last_message",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    name           TEXT PRIMARY KEY,
    source         TEXT NOT NULL UNIQUE,
    dest           TEXT NOT NULL,
    oncalendar     TEXT NOT NULL,
    schedule_human TEXT NOT NULL,
    keep           INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    last_run_at    TEXT,
    last_status    TEXT,
    last_message   TEXT
);
CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class Job:
    name: str
    source: str
    dest: str
    oncalendar: str
    schedule_human: str
    keep: int
    created_at: str
    last_run_at: Optional[str] = None
    last_status: Optional[str] = None
    last_message: Optional[str] = None


# Columns introduced after the initial release. connect() ensures each exists,
# so a jobs.db created by an older version is upgraded in place on open without
# losing data. Each column MUST be nullable or carry a DEFAULT — SQLite cannot
# ADD a NOT NULL column without a default to a populated table. Append future
# columns here; never remove or reorder existing entries.
_ADDED_COLUMNS = [
    # ("jobs", "future_col", "TEXT"),
]


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(
        row["name"] == column
        for row in conn.execute("PRAGMA table_info(%s)" % table)
    )


def _ensure_column(
    conn: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    if not _column_exists(conn, table, column):
        conn.execute(
            "ALTER TABLE %s ADD COLUMN %s %s" % (table, column, definition)
        )


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, definition in _ADDED_COLUMNS:
        _ensure_column(conn, table, column, definition)
    conn.commit()


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    if path is None:
        paths.ensure_dirs()
        path = paths.db_path()
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    _migrate(conn)
    return conn


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(**{f.name: row[f.name] for f in fields(Job)})


def add_job(conn: sqlite3.Connection, job: Job) -> None:
    try:
        conn.execute(
            "INSERT INTO jobs (name, source, dest, oncalendar, schedule_human, "
            "keep, created_at, last_run_at, last_status, last_message) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                job.name, job.source, job.dest, job.oncalendar,
                job.schedule_human, job.keep, job.created_at,
                job.last_run_at, job.last_status, job.last_message,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError(str(exc)) from exc
    conn.commit()


def get_job(conn: sqlite3.Connection, name: str) -> Optional[Job]:
    row = conn.execute("SELECT * FROM jobs WHERE name = ?", (name,)).fetchone()
    return _row_to_job(row) if row else None


def get_job_by_source(conn: sqlite3.Connection, source: str) -> Optional[Job]:
    row = conn.execute("SELECT * FROM jobs WHERE source = ?", (source,)).fetchone()
    return _row_to_job(row) if row else None


def list_jobs(conn: sqlite3.Connection) -> List[Job]:
    rows = conn.execute("SELECT * FROM jobs ORDER BY name").fetchall()
    return [_row_to_job(r) for r in rows]


def update_job(conn: sqlite3.Connection, name: str, /, **fields_: object) -> None:
    if not fields_:
        return
    unknown = set(fields_) - set(_COLUMNS)
    if unknown:
        raise ValueError("unknown column(s): %s" % ", ".join(sorted(unknown)))
    assignments = ", ".join("%s = ?" % col for col in fields_)
    try:
        conn.execute(
            "UPDATE jobs SET %s WHERE name = ?" % assignments,
            (*fields_.values(), name),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError(str(exc)) from exc
    conn.commit()


def remove_job(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute("DELETE FROM jobs WHERE name = ?", (name,))
    conn.commit()
    return cur.rowcount > 0


def record_run(
    conn: sqlite3.Connection, name: str, status: str, message: str, run_at: str
) -> None:
    update_job(
        conn, name,
        last_status=status, last_message=message, last_run_at=run_at,
    )


def get_config(
    conn: sqlite3.Connection, key: str, default: Optional[str] = None
) -> Optional[str]:
    row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_config(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO config (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
