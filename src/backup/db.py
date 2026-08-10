from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from . import paths

_COLUMNS = (
    "name", "source", "dest", "oncalendar", "schedule_human",
    "keep", "created_at", "last_run_at", "last_status", "last_message",
    "job_id", "last_snapshot", "blocked_reason", "archived_at",
    "archived_reason",
)

# Single source of truth for the jobs table column definitions. Used both to
# create a fresh table (_SCHEMA) and to rebuild it during migration
# (_rebuild_jobs_without_source_unique), so the two can never drift apart when a
# column is added. NOTE: `source` intentionally has no UNIQUE — a source may be
# backed up to several destinations; uniqueness is enforced on (source, dest).
_JOBS_TABLE_BODY = """
    name           TEXT PRIMARY KEY,
    source         TEXT NOT NULL,
    dest           TEXT NOT NULL,
    oncalendar     TEXT NOT NULL,
    schedule_human TEXT NOT NULL,
    keep           INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    last_run_at    TEXT,
    last_status    TEXT,
    last_message   TEXT,
    job_id         TEXT,
    last_snapshot  TEXT,
    blocked_reason TEXT,
    archived_at    TEXT,
    archived_reason TEXT
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (%s);
CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    job_name    TEXT NOT NULL,
    snapshot    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    file_count  INTEGER,
    total_bytes INTEGER,
    PRIMARY KEY (job_name, snapshot)
);
""" % _JOBS_TABLE_BODY


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
    job_id: Optional[str] = None
    last_snapshot: Optional[str] = None
    blocked_reason: Optional[str] = None
    archived_at: Optional[str] = None
    archived_reason: Optional[str] = None


# Columns introduced after the initial release. connect() ensures each exists,
# so a jobs.db created by an older version is upgraded in place on open without
# losing data. Each column MUST be nullable or carry a DEFAULT — SQLite cannot
# ADD a NOT NULL column without a default to a populated table. Append future
# columns here; never remove or reorder existing entries.
_ADDED_COLUMNS = [
    ("jobs", "job_id", "TEXT"),
    ("jobs", "last_snapshot", "TEXT"),
    ("jobs", "blocked_reason", "TEXT"),
    ("jobs", "archived_at", "TEXT"),
    ("jobs", "archived_reason", "TEXT"),
    ("snapshots", "file_count", "INTEGER"),
    ("snapshots", "total_bytes", "INTEGER"),
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


def _legacy_source_unique(conn: sqlite3.Connection) -> bool:
    """True if jobs.source still carries the old single-column UNIQUE."""
    for idx in conn.execute("PRAGMA index_list(jobs)"):
        if idx["origin"] != "u" or not idx["unique"]:
            continue
        cols = [r["name"] for r in conn.execute(
            "PRAGMA index_info(%s)" % idx["name"])]
        if cols == ["source"]:
            return True
    return False


def _rebuild_jobs_without_source_unique(conn: sqlite3.Connection) -> None:
    """Recreate `jobs` without the column-level UNIQUE on source, preserving rows."""
    cols = ", ".join(_COLUMNS)
    conn.execute("CREATE TABLE jobs_new (%s)" % _JOBS_TABLE_BODY)
    conn.execute("INSERT INTO jobs_new (%s) SELECT %s FROM jobs" % (cols, cols))
    conn.execute("DROP TABLE jobs")
    conn.execute("ALTER TABLE jobs_new RENAME TO jobs")


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, definition in _ADDED_COLUMNS:
        _ensure_column(conn, table, column, definition)
    conn.commit()
    if _legacy_source_unique(conn):
        conn.execute("BEGIN")
        try:
            _rebuild_jobs_without_source_unique(conn)
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS jobs_source_dest ON jobs(source, dest)"
    )
    conn.commit()


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    if path is None:
        paths.ensure_dirs()
        path = paths.db_path()
    # generous lock timeout: the TUI may read while a scheduled run writes
    conn = sqlite3.connect(str(path), timeout=30.0)
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
            "INSERT INTO jobs (%s) VALUES (%s)"
            % (", ".join(_COLUMNS), ", ".join("?" for _ in _COLUMNS)),
            tuple(getattr(job, col) for col in _COLUMNS),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError(str(exc)) from exc
    conn.commit()


def get_job(conn: sqlite3.Connection, name: str) -> Optional[Job]:
    row = conn.execute("SELECT * FROM jobs WHERE name = ?", (name,)).fetchone()
    return _row_to_job(row) if row else None


def list_jobs_by_source(conn: sqlite3.Connection, source: str) -> List[Job]:
    rows = conn.execute(
        "SELECT * FROM jobs WHERE source = ? ORDER BY name", (source,)
    ).fetchall()
    return [_row_to_job(r) for r in rows]


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
        if "name" in fields_:
            conn.execute(
                "UPDATE snapshots SET job_name = ? WHERE job_name = ?",
                (fields_["name"], name),
            )
    except sqlite3.IntegrityError as exc:
        raise ValueError(str(exc)) from exc
    conn.commit()


def remove_job(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute("DELETE FROM jobs WHERE name = ?", (name,))
    conn.execute("DELETE FROM snapshots WHERE job_name = ?", (name,))
    conn.commit()
    return cur.rowcount > 0


def record_run(
    conn: sqlite3.Connection, name: str, status: str, message: str, run_at: str
) -> None:
    update_job(
        conn, name,
        last_status=status, last_message=message, last_run_at=run_at,
    )


def record_snapshot(
    conn: sqlite3.Connection, job_name: str, snapshot: str,
    created_at: Optional[str] = None,
    file_count: Optional[int] = None, total_bytes: Optional[int] = None,
) -> None:
    if created_at is None:
        created_at = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT OR IGNORE INTO snapshots "
        "(job_name, snapshot, created_at, file_count, total_bytes) "
        "VALUES (?,?,?,?,?)",
        (job_name, snapshot, created_at, file_count, total_bytes),
    )
    if file_count is not None or total_bytes is not None:
        # A same-name snapshot may pre-exist (same-second rerun, backfill):
        # its stats must describe the directory as it stands now.
        conn.execute(
            "UPDATE snapshots SET file_count = ?, total_bytes = ? "
            "WHERE job_name = ? AND snapshot = ?",
            (file_count, total_bytes, job_name, snapshot),
        )
    conn.commit()


def get_snapshot_stats(
    conn: sqlite3.Connection, job_name: str, snapshot: str
):
    """(file_count, total_bytes) recorded at creation, or None if untracked.

    Either element may be None for snapshots recorded before stats existed.
    """
    row = conn.execute(
        "SELECT file_count, total_bytes FROM snapshots "
        "WHERE job_name = ? AND snapshot = ?",
        (job_name, snapshot),
    ).fetchone()
    if row is None:
        return None
    return (row["file_count"], row["total_bytes"])


def set_snapshot_stats(
    conn: sqlite3.Connection, job_name: str, snapshot: str,
    file_count: int, total_bytes: int,
) -> None:
    conn.execute(
        "UPDATE snapshots SET file_count = ?, total_bytes = ? "
        "WHERE job_name = ? AND snapshot = ?",
        (file_count, total_bytes, job_name, snapshot),
    )
    conn.commit()


def snapshot_stats_map(conn: sqlite3.Connection, job_name: str):
    """{snapshot: (file_count, total_bytes)} for every tracked snapshot."""
    rows = conn.execute(
        "SELECT snapshot, file_count, total_bytes FROM snapshots "
        "WHERE job_name = ?", (job_name,),
    ).fetchall()
    return {r["snapshot"]: (r["file_count"], r["total_bytes"]) for r in rows}


def forget_snapshot(conn: sqlite3.Connection, job_name: str, snapshot: str) -> None:
    conn.execute(
        "DELETE FROM snapshots WHERE job_name = ? AND snapshot = ?",
        (job_name, snapshot),
    )
    conn.commit()


def list_snapshot_names(conn: sqlite3.Connection, job_name: str) -> List[str]:
    rows = conn.execute(
        "SELECT snapshot FROM snapshots WHERE job_name = ? ORDER BY snapshot",
        (job_name,),
    ).fetchall()
    return [r["snapshot"] for r in rows]


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


def get_default_dests(conn: sqlite3.Connection) -> List[str]:
    """Default destinations, in insertion order.

    The value is a JSON array; a bare path written by an older version is
    read as a one-element list (rewritten as JSON on the next set).
    """
    raw = get_config(conn, "default_dest")
    if raw is None:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return [raw]
    if isinstance(parsed, list):
        return [str(p) for p in parsed]
    return [raw]


def set_default_dests(conn: sqlite3.Connection, dests: List[str]) -> None:
    set_config(conn, "default_dest", json.dumps(dests))
