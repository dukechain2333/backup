# .backupignore Support

**Status:** Approved design (2026-06-29)
**Builds on:** [2026-06-28-backup-design.md](2026-06-28-backup-design.md)

## Purpose

Let users exclude files/directories from a job's backups by dropping a
`.backupignore` file (gitignore-style) in the source tree, and add a
`backup preview` command to see what a run would back up after the ignore rules
are applied — without writing anything.

## Honoring .backupignore (nested, gitignore-style)

The rsync command gains one argument:

```
--filter=dir-merge,- .backupignore
```

This is rsync's native per-directory merge filter. For every directory rsync
walks, it reads a `.backupignore` (if present) and treats each line as an
**exclude pattern** scoped to that directory's subtree. So a `.backupignore` may
sit at the source root or in any subdirectory, exactly like `.gitignore`. With
no file present it is a no-op.

- Added unconditionally to every rsync invocation (normal run, forced run, and
  preview) — no new stored state.
- Pattern syntax is rsync's: `*`, `**`, `?`, `[...]`; a leading `/` anchors to
  that directory; a trailing `/` matches directories only; `#` comments and
  blank lines are ignored. Every line is an exclude (gitignore's `!` re-include
  is not supported in this mode — documented as the simplicity tradeoff).
- Because snapshots mirror the source (`--delete`), newly-ignored content
  disappears from *new* snapshots; older snapshots still retain it.
- `.backupignore` files are backed up by default; a user can exclude one by
  listing `.backupignore` in itself.

## Command: `backup preview <name>`

Runs rsync in dry-run against a throwaway empty directory with the same filter,
and prints the relative path of every file that *would* be backed up (source
minus ignored). Writes nothing to disk.

- Implemented as `runner.preview_backup(job) -> List[str]` so it shares the exact
  filter/argv logic with the real run.
- Uses `rsync -rn --filter='dir-merge,- .backupignore' --out-format='%n'
  <source>/ <empty-tmp>/`, returning the listed relative paths (excluding the
  bare `./` root entry, sorted).
- CLI `cmd_preview`: prints one path per line, or "nothing to back up" if empty;
  unknown job or missing source → clear error, non-zero exit.

## Scope / files

- `src/backup/runner.py` — define the filter once (`IGNORE_FILTER` constant);
  add it to the rsync argv in `run_backup`; add `preview_backup(job)`.
- `src/backup/cli.py` — `preview` subcommand wired to `cmd_preview`.
- `tests/test_runner.py` — ignored files (top-level AND nested) absent from a
  real snapshot while siblings are kept; `preview_backup` returns the included
  set and omits ignored.
- `tests/test_cli.py` — `preview` prints included files and omits ignored;
  unknown job errors.
- `README.md` — `.backupignore` section + `preview` usage.

## Error handling

- Missing source in `preview_backup` → return empty list / CLI reports the error
  (mirrors run's source-missing handling); never crash.
- A malformed `.backupignore` is rsync's domain; rsync ignores blank/`#` lines
  and applies the rest. No pre-validation by us.

## Testing

- `run_backup` with a source containing `keep.txt`, `secret.log`,
  `sub/keep2.txt`, `sub/tmp.cache`; root `.backupignore` = `*.log`;
  `sub/.backupignore` = `*.cache`. Snapshot contains `keep.txt`, `sub/keep2.txt`;
  excludes `secret.log` and `sub/tmp.cache`.
- `preview_backup` on the same tree returns `keep.txt` and `sub/keep2.txt`,
  not the ignored files, and writes nothing (destination untouched / no snapshot
  created).
- CLI `preview` prints the included files, omits ignored ones; unknown job → rc 1.

## Out of scope

gitignore `!` re-include semantics; a global/default ignore list; excluding the
`.backupignore` files automatically.
