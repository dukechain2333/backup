# Default Backup Destination

**Status:** Approved design (2026-06-29)
**Builds on:** [2026-06-28-backup-design.md](2026-06-28-backup-design.md)

## Purpose

Let the user set a default backup destination once, so adding a job becomes
`cd <dir> && backup add` with no flags. `--source` (default: cwd) and `--dest`
remain available as explicit overrides.

## Storage

Add a key/value `config` table to the existing SQLite DB:

```sql
CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

New `db` functions:
- `get_config(conn, key: str, default: Optional[str] = None) -> Optional[str]`
- `set_config(conn, key: str, value: str) -> None` (upsert)

One key is used for now: `default_dest`.

## Command: `backup config`

- `backup config` (no args) — print current settings, e.g.
  `default-dest: /mnt/backups` or `default-dest: (not set)`.
- `backup config --default-dest <path>` — resolve to an absolute path,
  `mkdir -p` it, store under `default_dest`, print a confirmation.

## `add`: destination becomes optional

`--dest` is no longer required. Destination resolution order:
1. `--dest <path>` if provided (always wins),
2. else the configured `default_dest`,
3. else error (exit non-zero):
   `no destination: pass --dest or set one with 'backup config --default-dest <path>'`.

All existing `add` behavior is unchanged: source still defaults to the current
directory; the resolved dest still goes through the dest-not-inside-source guard,
`mkdir -p`, and the duplicate name/source checks. Each job remains at
`<dest>/<name>/…`, so multiple jobs sharing one default dest coexist by name.

## Testing

- `db`: `set_config`/`get_config` round-trip; upsert overwrites; missing key
  returns the default.
- `config` command: setting `--default-dest` persists and creates the dir;
  no-arg shows the value and `(not set)` when absent.
- `add`: uses `default_dest` when `--dest` omitted; explicit `--dest` overrides
  the default; errors clearly when neither is set.

## Out of scope

No other config keys, no per-job dest defaults, no global config file (the DB
holds it).
