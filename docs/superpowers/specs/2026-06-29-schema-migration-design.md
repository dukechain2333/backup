# Schema Migration Safety Net

**Status:** Approved design (2026-06-29)
**Builds on:** [2026-06-28-backup-design.md](2026-06-28-backup-design.md)

## Purpose

Guarantee that a `backup` version update never loses or breaks an existing
`jobs.db`, even when a new release **adds a column** to an existing table.
New *tables* already upgrade cleanly via `CREATE TABLE IF NOT EXISTS`; new
*columns* do not, so this adds that missing piece.

## Mechanism

In `db.connect()`, after the `CREATE TABLE IF NOT EXISTS` schema runs, apply an
idempotent additive migration step that ensures every expected column exists,
adding any that are missing with `ALTER TABLE ... ADD COLUMN`.

```python
def _column_exists(conn, table, column) -> bool:
    return any(r["name"] == column
              for r in conn.execute("PRAGMA table_info(%s)" % table))

def _ensure_column(conn, table, column, definition) -> None:
    if not _column_exists(conn, table, column):
        conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, definition))

# Columns introduced after the initial release. Each MUST be nullable or carry
# a DEFAULT — SQLite cannot ADD a NOT NULL column without a default to a
# populated table. Append future columns here; never remove or reorder.
_ADDED_COLUMNS = [
    # ("jobs", "future_col", "TEXT"),
]

def _migrate(conn) -> None:
    for table, column, definition in _ADDED_COLUMNS:
        _ensure_column(conn, table, column, definition)
    conn.commit()
```

`connect()` calls `_migrate(conn)` after `executescript(_SCHEMA)`.

## Why not a version counter

A `PRAGMA user_version` + ordered migration list is the heavier classic
approach. For *additive column* safety the column registry above is simpler,
self-describing, and self-healing (it converges to the right schema regardless
of the DB's prior state), with the same guarantee. Non-additive changes
(renames, data transforms) are out of scope and would warrant the version-counter
approach if ever needed.

## Constraints

- Idempotent: running `connect()` repeatedly never errors or duplicates work.
- Additive only: columns must be nullable / defaulted; never drop or rename.
- No data loss: existing rows are preserved untouched.

## Testing

- `_ensure_column` adds a missing column, is idempotent on a second call, and
  preserves existing rows.
- `_column_exists` reports presence/absence correctly.
- Integration: create a DB with the current schema, insert a job, then (with a
  test-injected `_ADDED_COLUMNS` entry) reopen via `connect()` and confirm the
  new column appears and the job's data survives.
