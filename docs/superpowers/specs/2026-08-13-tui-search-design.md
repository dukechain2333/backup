# TUI task search design

Date: 2026-08-13

## Goal

Find a task quickly in `backup tui`: press `/`, type a few characters, and
the Tasks column narrows to matching tasks as you type (fzf-style incremental
filter). Confirm to land on the task with the full list restored, ready for
the usual keys (`r`, `v`, `a`, …).

## Scope

- Matches **task labels only** (what the left column shows), with
  case-insensitive contiguous substring matching: `sagnn` matches
  `hyper-sagnn` and `hypersagnn-dn`; `hsd` matches nothing.
- Search is pure presentation state. `Model`, `load_model`, `ops`, and `db`
  are untouched; all changes live in `tui.py`.

## Interaction

- `/` (from any column) enters search mode: focus jumps to the Tasks column
  and the pre-search selection is remembered.
- While searching:
  - printable characters append to the query, backspace deletes; every
    keystroke re-filters and resets the selection to the first match;
  - `↑`/`↓` move between matches (`j`/`k` are query characters here, as in
    fzf);
  - the middle/right columns follow the filtered selection as usual.
- `Enter` confirms: leave search mode, **clear the filter**, restore the full
  list with the selection kept on the chosen task.
- `Esc` cancels: leave search mode, clear the query, restore the pre-search
  selection.
- No matches: both sections show `(none)`; `Enter` behaves like `Esc`.

## Rendering

- In search mode the bottom bar shows ` /<query>▌  (N matches)` instead of
  the help line; the match count is across both sections.
- ACTIVE/ARCHIVED section headers stay; each section lists only its matching
  tasks.
- `_HELP` gains `[/] search`.

## Implementation notes

- `App` gains `searching: bool`, `search_query: str`, and a remembered
  pre-search source path for `Esc`.
- New module-level `_match(label: str, query: str) -> bool` (lowercased
  substring test; empty query matches everything).
- `App.tasks` property returns the filtered view of `model.tasks`; the
  selection helpers (`task`, `_clamp`), `move`, and `_draw` switch from
  `model.tasks` to it. `refresh_model`'s selection-restore also looks up the
  filtered list, so `g` while searching keeps working.
- The main loop routes keys to a search handler first when `searching` is
  true; `Ui.prompt` (blocking, bar-only redraw) is not reused — incremental
  filtering needs a full `_draw` per keystroke.

## Error handling

No IO and no failure paths. The only edge is an empty filtered list, covered
by `_clamp` guarding against empty `App.tasks` (selection indices pin to 0,
`task`/`dest`/`snap` return None as today).

## Testing

- Unit tests for `_match` (case-insensitivity, empty query, no-match) and for
  the filtered `App.tasks` / `_clamp` interaction (selection stays in bounds
  when the filter empties the list), instantiating `App` with a hand-built
  `Model` — no curses needed.
- The curses key loop stays manually verified, like the rest of the TUI.
