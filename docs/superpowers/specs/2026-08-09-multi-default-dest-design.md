# Multiple Default Backup Destinations

**Status:** Approved design (2026-08-09)
**Builds on:** [2026-06-29-default-dest-design.md](2026-06-29-default-dest-design.md),
[2026-07-01-fanout-backups-design.md](2026-07-01-fanout-backups-design.md)

## Purpose

Let the user configure several default destinations (e.g. a NAS and a USB
drive), so that `backup add` with no `--dest` creates one job per default
destination — every new source is automatically backed up to all of them.
Today only one `default_dest` can be stored; setting a new one overwrites it.

## Storage

Reuse the existing `config` table and the `default_dest` key. The value
becomes a JSON array of absolute paths, in insertion order.

New `db` functions:

- `get_default_dests(conn) -> List[str]` — reads `default_dest`. Missing key
  → `[]`. If the value parses as a JSON list, return it; otherwise treat it
  as a legacy single path and return `[value]`. No eager migration: old DBs
  keep working read-only, and the first write rewrites the key as JSON.
- `set_default_dests(conn, paths: List[str]) -> None` — stores
  `json.dumps(paths)` via the existing `set_config` upsert.

`get_config`/`set_config` remain unchanged; `cmd_add`/`cmd_config` stop
calling them directly for this key.

## Command: `backup config`

Three mutually exclusive options (argparse mutually exclusive group), plus
the existing no-arg display:

- `backup config` — print `default-dest: a, b` (comma-joined, insertion
  order) or `default-dest: (not set)`.
- `backup config --add-default-dest <path>` — resolve to absolute,
  `mkdir -p`, append to the list. If already present, print a note and exit
  0 (idempotent). Print the resulting list.
- `backup config --remove-default-dest <path>` — resolve, remove from the
  list. Not in the list → error, exit 1. Does not touch the directory
  itself. Print the resulting list.
- `backup config --default-dest <path>` — kept for backward compatibility;
  now means "replace the whole list with this one path" (resolve + mkdir as
  today). Single-destination users see no behavior change.

## `add`: fan-out over defaults

With `--dest` given, behavior is unchanged (one job, defaults ignored).
Without `--dest`:

- **0 defaults** — same error as today; hint mentions both
  `--default-dest` and `--add-default-dest`.
- **1 default** — exactly today's behavior; job name gets no suffix.
- **N ≥ 2 defaults** — create one job per destination, in list order:
  1. Validate once: job name charset, `--keep >= 1`, schedule parse +
     systemd validation, source control-character check.
  2. Check **all** destinations for inside-source/equal-source upfront; if
     any fails, error out before creating anything (atomic — no partial
     batch).
  3. Skip destinations where the `(source, dest)` job already exists, with
     a note on stderr naming the existing job. If all are skipped, error
     exit 1 ("source already backed up to all default destinations").
  4. If the source already has jobs at other destinations, run the existing
     `_confirm_duplicate_source` prompt once for the whole batch.
  5. Naming: `base` = `--name` or `slugify(source.name)`. Each job is named
     `<base>-<slugify(dest basename)>`; on collision (in the DB or within
     the batch) append `-2`, `-3`, … until free — the batch never fails on
     a name clash. The existing name-clash error (with its `--name` hint)
     remains only in the 0/1-default and explicit `--dest` paths.
  6. Per job: `mkdir -p` dest, insert DB row, install systemd units. A unit
     install failure rolls back that job's DB row (existing logic) and the
     loop continues; exit 1 at the end if any job failed, 0 otherwise —
     same "continue past failure and summarize" style as `run --all`.
  7. Print one `added job ...` line per created job.

`edit`, `remove`, `run`, `restore`, `verify` are untouched — they operate on
already-created jobs.

## Testing

Follow existing pytest patterns (`xdg` fixture, monkeypatch, capsys).

- `test_db`: `get_default_dests` on missing key, JSON value, and legacy
  plain-path value; `set_default_dests` round-trip and overwrite.
- `test_cli`:
  - config: add appends, add is idempotent, remove works, remove of unknown
    path errors, `--default-dest` replaces the whole list, display formats.
  - add fan-out: N defaults → N jobs with `<base>-<destslug>` names;
    dest-basename collision gets numeric suffix; already-existing
    (source, dest) skipped with note; all-existing → exit 1; any default
    inside source → nothing created; 1 default → unsuffixed name
    (regression); unit-install failure on one dest rolls back only that job
    and exits 1.
- Existing tests (e.g. `test_add_uses_default_dest_when_dest_omitted`,
  `test_config_sets_and_shows_default_dest`) keep passing; the latter's
  stored-value assertion is updated to the JSON form.
