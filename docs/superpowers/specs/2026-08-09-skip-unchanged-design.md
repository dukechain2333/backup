# Skip-if-unchanged design

Date: 2026-08-09
Status: approved (user picked this over layered retention)

## Problem

Every scheduled run rotates in a new snapshot regardless of whether the source
changed. With the default `daily@02:00, keep 7`, a week without changes leaves
seven identical snapshots and prunes away the last snapshot that differed —
the retention window measures days, not meaningful versions. Disk cost is nil
(hard links) but history is silently lost.

## Decision

Before the real rsync, dry-run rsync against the newest snapshot:

    rsync -a -n -i --delete <ignore-filters> SOURCE/ PREV_SNAPSHOT/

- Empty itemized output and exit code in {0, 24} → **no changes**: skip the
  run entirely. No `.partial`, no rotation, no prune, no `latest` update,
  no `last_snapshot` update. The run is still recorded (`last_run_at`,
  `last_status`) with new status `unchanged`, and logged.
- Any output, or a dry-run error → proceed with the normal full backup.
  False positives cost one redundant (hard-linked) snapshot; false negatives
  would lose a backup, so all ambiguity resolves toward running.

Never skipped when:
- there is no previous snapshot (first run / baseline), or
- `--force` is given (retains its re-baseline semantics).

Ordering: archived check → blocked check → integrity verify → **unchanged
check** → full backup. Verification still runs on unchanged days so
destination corruption is still caught.

## Status plumbing

New `RunResult.status` value `unchanged` (snapshot=None). CLI treats it as
success: exit 0 for single runs and `--internal-run`; `run --all` counts it
separately and appends `, N unchanged` to the summary when nonzero.

## Test impact

Existing tests that assumed "every run creates a snapshot" now modify the
source between runs (hardlink, retention, latest-symlink, same-second tests).
New tests cover: skip on unchanged source, deletion detected as change,
change after a skipped run, `--force` bypassing the skip, CLI exit codes and
`run --all` summary.
