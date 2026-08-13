# TUI Task Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `/` in `backup tui` incrementally filters the Tasks column by a
case-insensitive substring of the task label; Enter lands on the chosen task
with the full list restored, Esc cancels.

**Architecture:** Search is pure presentation state inside `tui.py`. `App`
gains a query string and a `tasks` property returning the filtered view of
`model.tasks`; the selection helpers, key routing, and drawing switch to that
property. `Model`, `load_model`, `ops`, and `db` are untouched. Spec:
`docs/superpowers/specs/2026-08-13-tui-search-design.md`.

**Tech Stack:** Python stdlib only (`curses`, `dataclasses`); pytest for
tests. The package must stay dependency-free.

## Global Constraints

- No new dependencies; TUI uses stdlib `curses` only.
- All production changes live in `src/backup/tui.py`; tests in
  `tests/test_tui.py`.
- Matching is a case-insensitive **contiguous substring** on the task label
  only (no fuzzy/subsequence matching, no paths).
- Search never touches the DB or `ops`; no IO in any new code path.
- Run tests with `python -m pytest tests/test_tui.py -v` from the repo root
  (full suite: `python -m pytest`).

---

### Task 1: `_match` and the filtered `App.tasks` view

**Files:**
- Modify: `src/backup/tui.py` (App `__init__` ~line 101, selection helpers
  ~lines 113-153, new module-level function near `_state_color`)
- Test: `tests/test_tui.py`

**Interfaces:**
- Produces: `tui._match(label: str, query: str) -> bool`;
  `App.search_query: str` attribute (`""` = no filter);
  `App.tasks` property `-> List[TaskView]` — the filtered task list that
  `task`/`_clamp`/`refresh_model` now index into. Task 2 and 3 rely on all
  three names exactly as written.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tui.py`:

```python
# ---------------------------------------------------------------- search


def _task(label, archived=False):
    return tui.TaskView(source="/src/" + label, label=label,
                        archived=archived)


def _filter_app(labels, query=""):
    """App over a hand-built model; conn unused by the filter code paths."""
    app = tui.App(None)
    app.model = tui.Model(tasks=[_task(l) for l in labels])
    app.search_query = query
    return app


def test_match_is_case_insensitive_substring():
    assert tui._match("hyper-sagnn", "SAGNN")
    assert tui._match("hypersagnn-dn", "sagnn")
    assert not tui._match("hypersagnn-dn", "hsd")   # no fuzzy subsequence
    assert tui._match("anything", "")               # empty query matches all


def test_tasks_property_filters_by_query():
    app = _filter_app(["esco", "hyper-sagnn", "hypersagnn-dn"], "sagnn")
    assert [t.label for t in app.tasks] == ["hyper-sagnn", "hypersagnn-dn"]
    app.search_query = ""
    assert [t.label for t in app.tasks] == [
        "esco", "hyper-sagnn", "hypersagnn-dn"]


def test_selection_stays_in_bounds_when_filter_empties_list():
    app = _filter_app(["esco", "metacell"])
    app.task_i = 1
    app.search_query = "zzz"
    app._clamp()
    assert app.tasks == []
    assert app.task is None
    assert app.dest is None
    assert app.snap is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_tui.py -k "match or tasks_property or in_bounds" -v`
Expected: FAIL — `AttributeError: module 'backup.tui' has no attribute '_match'` (and the App tests fail on the missing `tasks` property).

- [ ] **Step 3: Implement**

In `src/backup/tui.py`:

(a) Module-level function, directly below `_state_color` (line 91-97):

```python
def _match(label: str, query: str) -> bool:
    """Case-insensitive contiguous substring; empty query matches all."""
    return query.lower() in label.lower()
```

(b) In `App.__init__` (after `self.message_color = _C_NORMAL`, line 109):

```python
        self.search_query = ""         # task filter; "" shows all tasks
```

(c) New property at the top of the "selection helpers" block (before the
`task` property, line 113):

```python
    @property
    def tasks(self) -> List[TaskView]:
        """Tasks visible under the current search filter."""
        if not self.search_query:
            return self.model.tasks
        return [t for t in self.model.tasks
                if _match(t.label, self.search_query)]
```

(d) Switch the three selection code paths from `self.model.tasks` to
`self.tasks`:

- `task` property (line 115): `if 0 <= self.task_i < len(self.tasks): return self.tasks[self.task_i]`
- `refresh_model` (line 138): `for i, t in enumerate(self.tasks):`
- `_clamp` (line 149): `self.task_i = max(0, min(self.task_i, len(self.tasks) - 1))`

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_tui.py -v`
Expected: all PASS (new tests plus every pre-existing test — the switch must
not change behavior while `search_query` is empty).

- [ ] **Step 5: Commit**

```bash
git add src/backup/tui.py tests/test_tui.py
git commit -m "feat(tui): filtered task view behind App.tasks"
```

---

### Task 2: search-mode state machine on App

**Files:**
- Modify: `src/backup/tui.py` (App `__init__`, new methods after `focus`,
  ~line 173)
- Test: `tests/test_tui.py`

**Interfaces:**
- Consumes: `App.tasks`, `App.search_query`, `tui._match` from Task 1.
- Produces: `App.searching: bool`; `App.start_search() -> None`;
  `App.accept_search() -> None`; `App.cancel_search() -> None`;
  `App.search_key(ch: int) -> None` (takes a raw `curses` key code).
  Task 3's main loop calls `start_search` and `search_key` exactly so.

- [ ] **Step 1: Write the failing tests**

Add `import curses` to the imports of `tests/test_tui.py`, then append:

```python
def _typing(app, text):
    for ch in text:
        app.search_key(ord(ch))


def test_search_typing_filters_and_resets_selection():
    app = _filter_app(["esco", "hyper-sagnn", "hypersagnn-dn"])
    app.task_i = 2
    app.col = 1
    app.start_search()
    assert app.searching is True
    assert app.col == 0
    _typing(app, "sagnn")
    assert app.search_query == "sagnn"
    assert [t.label for t in app.tasks] == ["hyper-sagnn", "hypersagnn-dn"]
    assert app.task.label == "hyper-sagnn"      # reset to first match


def test_search_enter_lands_on_chosen_task_with_full_list():
    app = _filter_app(["esco", "hyper-sagnn", "hypersagnn-dn"])
    app.start_search()
    _typing(app, "sagnn")
    app.search_key(curses.KEY_DOWN)             # move to hypersagnn-dn
    app.search_key(10)                          # Enter
    assert app.searching is False
    assert app.search_query == ""               # filter cleared
    assert len(app.tasks) == 3                  # full list restored
    assert app.task.label == "hypersagnn-dn"    # selection kept


def test_search_esc_restores_presearch_selection():
    app = _filter_app(["esco", "hyper-sagnn", "hypersagnn-dn"])
    app.task_i = 1                              # hyper-sagnn selected
    app.start_search()
    _typing(app, "esco")
    app.search_key(27)                          # Esc
    assert app.searching is False
    assert app.search_query == ""
    assert app.task.label == "hyper-sagnn"


def test_search_enter_with_no_match_behaves_like_esc():
    app = _filter_app(["esco", "metacell"])
    app.task_i = 1
    app.start_search()
    _typing(app, "zzz")
    assert app.tasks == []
    app.search_key(10)                          # Enter on empty result
    assert app.searching is False
    assert app.task.label == "metacell"


def test_search_backspace_refilters():
    app = _filter_app(["esco", "metacell"])
    app.start_search()
    _typing(app, "mz")
    assert app.tasks == []
    app.search_key(curses.KEY_BACKSPACE)
    assert [t.label for t in app.tasks] == ["metacell"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_tui.py -k search -v`
Expected: FAIL — `AttributeError: 'App' object has no attribute 'start_search'`.

- [ ] **Step 3: Implement**

In `src/backup/tui.py`:

(a) In `App.__init__`, extend the block added in Task 1:

```python
        self.search_query = ""         # task filter; "" shows all tasks
        self.searching = False         # search input mode active
        self._presearch_source = None  # selection to restore on Esc
```

(b) New section after the `focus` method (line 172-173), before
"---- actions":

```python
    # ---- search

    def start_search(self) -> None:
        self.searching = True
        self.search_query = ""
        self._presearch_source = self.task.source if self.task else None
        self.col = 0
        self.task_i = self.dest_i = self.snap_i = 0

    def _select_source(self, source: Optional[str]) -> None:
        if source is not None:
            for i, t in enumerate(self.tasks):
                if t.source == source:
                    self.task_i = i
                    break
        self._clamp()

    def accept_search(self) -> None:
        chosen = self.task.source if self.task else self._presearch_source
        self.searching = False
        self.search_query = ""
        self._select_source(chosen)

    def cancel_search(self) -> None:
        self.searching = False
        self.search_query = ""
        self._select_source(self._presearch_source)

    def search_key(self, ch: int) -> None:
        if ch in (curses.KEY_ENTER, 10, 13):
            self.accept_search()
        elif ch == 27:                                    # Esc
            self.cancel_search()
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            self.search_query = self.search_query[:-1]
            self.task_i = self.dest_i = self.snap_i = 0
            self._clamp()
        elif ch == curses.KEY_DOWN:
            self.move(1)
        elif ch == curses.KEY_UP:
            self.move(-1)
        elif 32 <= ch < 127:                              # printable ASCII
            self.search_query += chr(ch)
            self.task_i = self.dest_i = self.snap_i = 0
            self._clamp()
```

(`j`/`k` fall into the printable branch and become query characters — the
fzf convention the spec calls for; arrow keys navigate matches.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_tui.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/backup/tui.py tests/test_tui.py
git commit -m "feat(tui): search-mode state machine (/, Enter, Esc)"
```

---

### Task 3: key routing, rendering, docs

**Files:**
- Modify: `src/backup/tui.py` (`_HELP` line 87-88, `_draw` lines 504-524 and
  592-596, `_run` lines 600-640)
- Modify: `README.md:98-101` (TUI key list)

**Interfaces:**
- Consumes: `App.tasks`, `App.searching`, `App.search_query`,
  `App.start_search()`, `App.search_key(ch)` from Tasks 1-2. No new names
  produced; this task is wiring plus rendering.

- [ ] **Step 1: Route keys in the main loop**

In `_run`, insert the search branch immediately after `ch = scr.getch()`
and **before** the quit check (while searching, `q` must be a query
character, not quit), and bind `/`:

```python
    while True:
        _draw(scr, app)
        ch = scr.getch()
        if app.searching:
            app.message = ""
            app.search_key(ch)
            continue
        if ch in (ord("q"), ord("Q")):
            break
        app.message = ""
        if ch == ord("/"):
            app.start_search()
        elif ch in (curses.KEY_DOWN, ord("j")):
            app.move(1)
```

(The remaining `elif` branches — up/left/right/tab/g/r/v/a/i/u/d/RESIZE —
stay exactly as they are; only the shown lines change: the `searching`
branch is new, the `if`/`elif` chain now starts with `/`, and the former
`if ch in (curses.KEY_DOWN, ...)` becomes an `elif`.)

Also make Esc respond instantly where supported — in `_run`, after
`scr.keypad(True)` (line 602):

```python
    if hasattr(curses, "set_escdelay"):    # Python >= 3.9
        curses.set_escdelay(25)
```

- [ ] **Step 2: Draw the filtered list and the search bar**

In `_draw`:

(a) Sections (lines 504-505) — build from the filtered view:

```python
    sections = [("ACTIVE", [t for t in app.tasks if not t.archived]),
                ("ARCHIVED", [t for t in app.tasks if t.archived])]
```

(b) Row index (line 513) — index into the same filtered list:

```python
            i = app.tasks.index(t)
```

(c) Status bar (lines 593-596) — search input replaces message/help:

```python
    if app.searching:
        n = len(app.tasks)
        status = "/%s▌  (%d match%s)" % (app.search_query, n,
                                         "" if n == 1 else "es")
        scr.addnstr(h - 1, 0, _truncate(status, w - 1), w - 1,
                    curses.color_pair(_C_YELLOW) | curses.A_BOLD)
    else:
        status = app.message or _HELP
        scr.addnstr(h - 1, 0, _truncate(status, w - 1), w - 1,
                    curses.color_pair(app.message_color) if app.message
                    else curses.A_DIM)
```

(d) `_HELP` (lines 87-88):

```python
_HELP = ("[/] search  [Enter/←→] navigate  [r]estore  [v]erify  [a]rchive  "
         "[u]narchive  [i]mport  [d]elete  [g] refresh  [q]uit")
```

- [ ] **Step 3: Run the full test suite**

Run: `python -m pytest`
Expected: all PASS (rendering changes are exercised indirectly — every
pre-existing TUI test still passes because an empty query filters nothing).

- [ ] **Step 4: Manual smoke test**

Run `backup tui` in a real terminal and verify against the spec:

1. `/` from each column → focus jumps to Tasks, bar shows `/▌  (N matches)`.
2. Typing narrows both sections live; count updates; selection sits on the
   first match; middle/right columns follow it.
3. `↓`/`↑` move between matches; typing `j` appends to the query instead of
   moving.
4. Enter → full list back, selection on the chosen task; `r`/`v`/`a` act on
   it.
5. Esc → full list back, selection where it was before `/`.
6. Query with no matches → both sections show `(none)`, Enter behaves like
   Esc.
7. `g` while searching refreshes without crashing and keeps the filter.

- [ ] **Step 5: Update README**

Replace the key list sentence (README.md lines 98-101) with:

```markdown
Keys: arrows / `hjkl` / Tab to navigate, `/` incremental task search (type
to filter, Enter to jump to the match, Esc to cancel), `r` restore, `v`
verify snapshot fingerprints, `a` archive, `u` unarchive, `i` import an
existing backup location (prompts for a path; same detection as
`backup import`), `d` delete an archived task (choosing keep or purge
snapshots), `g` refresh, `q` quit.
```

- [ ] **Step 6: Commit**

```bash
git add src/backup/tui.py README.md
git commit -m "feat(tui): '/' incremental task search"
```
