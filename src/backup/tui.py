"""Interactive curses front-end: tasks / destinations / snapshots.

Layout — three Miller columns plus a status bar:

  left    tasks (jobs grouped by source), ACTIVE and ARCHIVED sections
  middle  destinations (one job per row) of the selected task
  right   snapshots of the selected destination; DB-recorded snapshots
          missing on disk are marked LOST

All state-changing work is delegated to `ops`; this module only renders and
routes keys.
"""
from __future__ import annotations

import curses
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from . import db, ops, units


# ---------------------------------------------------------------- model


@dataclass
class DestView:
    job: db.Job
    state: str                     # archived | blocked | active | paused
    snapshots: List[ops.SnapshotInfo] = field(default_factory=list)

    @property
    def lost(self) -> bool:
        """No snapshot of this destination is present on disk."""
        return not any(s.on_disk for s in self.snapshots)


@dataclass
class TaskView:
    source: str
    label: str
    archived: bool
    dests: List[DestView] = field(default_factory=list)

    @property
    def lost(self) -> bool:
        """Archive lost: not a single snapshot found at any destination."""
        return all(d.lost for d in self.dests)


@dataclass
class Model:
    tasks: List[TaskView] = field(default_factory=list)
    auto_archived: List[str] = field(default_factory=list)


def load_model(conn) -> Model:
    jobs = db.list_jobs(conn)
    auto = ops.auto_archive_missing_sources(conn, jobs)
    model = Model(auto_archived=auto)
    for task in ops.group_tasks(jobs):
        view = TaskView(source=task.source, label=task.label,
                        archived=task.archived)
        for job in task.jobs:
            view.dests.append(DestView(
                job=job,
                state=ops.job_state(job),
                snapshots=ops.snapshot_status(conn, job),
            ))
        model.tasks.append(view)
    # active tasks first, then archived, alphabetical inside each section
    model.tasks.sort(key=lambda t: (t.archived, t.label, t.source))
    return model


# ---------------------------------------------------------------- ui


_C_NORMAL = 0
_C_SEL = 1
_C_RED = 2
_C_GREEN = 3
_C_YELLOW = 4
_C_DIM = 5
_C_SEL_RED = 6

_HELP = ("[Enter/←→] navigate  [r]estore  [v]erify  [a]rchive  [u]narchive  "
         "[i]mport  [d]elete  [g] refresh  [q]uit")


def _state_color(state: str) -> int:
    return {
        "active": _C_GREEN,
        "paused": _C_YELLOW,
        "archived": _C_YELLOW,
        "blocked": _C_RED,
    }.get(state, _C_NORMAL)


def _match(label: str, query: str) -> bool:
    """Case-insensitive contiguous substring; empty query matches all."""
    return query.lower() in label.lower()


class App:
    def __init__(self, conn):
        self.conn = conn
        self.model = Model()
        self.col = 0                   # focused column: 0 tasks, 1 dests, 2 snaps
        self.task_i = 0
        self.dest_i = 0
        self.snap_i = 0
        self.message = ""
        self.message_color = _C_NORMAL
        self.search_query = ""         # task filter; "" shows all tasks

    # ---- selection helpers

    @property
    def tasks(self) -> List[TaskView]:
        """Tasks visible under the current search filter."""
        if not self.search_query:
            return self.model.tasks
        return [t for t in self.model.tasks
                if _match(t.label, self.search_query)]

    @property
    def task(self) -> Optional[TaskView]:
        if 0 <= self.task_i < len(self.tasks):
            return self.tasks[self.task_i]
        return None

    @property
    def dest(self) -> Optional[DestView]:
        task = self.task
        if task and 0 <= self.dest_i < len(task.dests):
            return task.dests[self.dest_i]
        return None

    @property
    def snap(self) -> Optional[ops.SnapshotInfo]:
        dest = self.dest
        if dest and 0 <= self.snap_i < len(dest.snapshots):
            return dest.snapshots[self.snap_i]
        return None

    def refresh_model(self, keep_selection: bool = True) -> None:
        selected = self.task.source if (keep_selection and self.task) else None
        self.model = load_model(self.conn)
        self.task_i = 0
        if selected is not None:
            for i, t in enumerate(self.tasks):
                if t.source == selected:
                    self.task_i = i
                    break
        self.dest_i = min(self.dest_i, max(0, len(self.task.dests) - 1)) if self.task else 0
        self._clamp()
        if self.model.auto_archived:
            self.note("auto-archived (source missing): %s"
                      % ", ".join(self.model.auto_archived), _C_YELLOW)

    def _clamp(self) -> None:
        self.task_i = max(0, min(self.task_i, len(self.tasks) - 1))
        n_dest = len(self.task.dests) if self.task else 0
        self.dest_i = max(0, min(self.dest_i, n_dest - 1))
        n_snap = len(self.dest.snapshots) if self.dest else 0
        self.snap_i = max(0, min(self.snap_i, n_snap - 1))

    def note(self, msg: str, color: int = _C_NORMAL) -> None:
        self.message = msg
        self.message_color = color

    # ---- key handling

    def move(self, delta: int) -> None:
        if self.col == 0:
            self.task_i += delta
            self.dest_i = self.snap_i = 0
        elif self.col == 1:
            self.dest_i += delta
            self.snap_i = 0
        else:
            self.snap_i += delta
        self._clamp()

    def focus(self, col: int) -> None:
        self.col = max(0, min(2, col))

    # ---- actions

    def do_archive(self, ui) -> None:
        task = self.task
        if task is None:
            return
        pending = [d.job for d in task.dests if d.job.archived_at is None]
        if not pending:
            self.note("task is already archived", _C_YELLOW)
            return
        if not ui.confirm("Archive %r (stop its timer, keep all snapshots)? [y/N]"
                          % task.label):
            return
        for job in pending:
            ops.archive_job(self.conn, job)
        self.refresh_model()
        self.note("archived %r (%d job%s); snapshots kept"
                  % (task.label, len(pending), "s" if len(pending) > 1 else ""),
                  _C_YELLOW)

    def do_unarchive(self, ui) -> None:
        task = self.task
        if task is None:
            return
        pending = [d.job for d in task.dests if d.job.archived_at is not None]
        if not pending:
            self.note("task is not archived", _C_YELLOW)
            return
        done, errors = 0, []
        for job in pending:
            try:
                ops.unarchive_job(self.conn, job)
                done += 1
            except (ValueError, RuntimeError) as exc:
                errors.append(str(exc))
        self.refresh_model()
        if errors:
            self.note("unarchive: %s" % errors[0], _C_RED)
        else:
            self.note("unarchived %r (%d job%s)"
                      % (task.label, done, "s" if done > 1 else ""), _C_GREEN)

    def do_delete(self, ui) -> None:
        task = self.task
        if task is None:
            return
        if not task.archived:
            self.note("only archived tasks can be deleted (archive it first with 'a')",
                      _C_YELLOW)
            return
        choice = ui.choose(
            "Delete archived task %r? [k] delete + keep snapshots  "
            "[p] delete + PURGE snapshots  [c] cancel" % task.label, "kpc")
        if choice not in ("k", "p"):
            return
        errors = []
        for dest in task.dests:
            try:
                ops.delete_job(self.conn, dest.job, purge=(choice == "p"))
            except ValueError as exc:
                errors.append(str(exc))
        self.refresh_model(keep_selection=False)
        if errors:
            self.note(errors[0], _C_RED)
        else:
            self.note("deleted task %r%s" % (
                task.label, " and purged its snapshots" if choice == "p" else
                "; snapshots kept on disk"), _C_YELLOW)

    def do_import(self, ui) -> None:
        path = ui.prompt("Import backup location (job dir or dest dir): ", "")
        if path is None or not path.strip():
            self.note("import cancelled")
            return
        p = Path(path.strip()).expanduser()
        try:
            kind, dirs = ops.scan_import_path(p)
        except ValueError as exc:
            self.note("import: %s" % exc, _C_RED)
            return
        imported, skipped = 0, 0
        for d in dirs:
            name, _ = ops.import_job_dir(self.conn, d)
            if name is None:
                skipped += 1
            else:
                imported += 1
        form = ("job directory" if kind == "job"
                else "destination directory with %d job dirs" % len(dirs))
        self.refresh_model()
        self.note(
            "import %s: %d imported, %d skipped — imported jobs are "
            "archived; fix source via CLI edit, then [u]narchive"
            % (form, imported, skipped),
            _C_GREEN if imported else _C_YELLOW)

    def do_restore(self, ui) -> None:
        dest = self.dest
        if dest is None:
            self.note("nothing to restore", _C_YELLOW)
            return
        if self.col == 2 and self.snap is not None:
            snap = self.snap
            if snap.lost:
                self.note("cannot restore %s: snapshot is LOST (missing on disk)"
                          % snap.name, _C_RED)
                return
        elif self.col == 0:
            # Task focused: newest snapshot present at ANY destination.
            dest, snap = None, None
            for d in self.task.dests:
                for s in d.snapshots:
                    if s.on_disk and (snap is None or s.name > snap.name):
                        dest, snap = d, s
            if snap is None:
                self.note("no snapshot present on disk for this task", _C_RED)
                return
        else:
            snap = next((s for s in dest.snapshots if s.on_disk), None)
            if snap is None:
                self.note("no snapshot present on disk for this destination", _C_RED)
                return
        default = str(ops.default_restore_target(dest.job))
        target = ui.prompt("Restore %s to: " % snap.name, default)
        if target is None or not target.strip():
            self.note("restore cancelled")
            return
        target_path = Path(target.strip()).expanduser()
        overwrites = ops.count_overwrites(dest.job, snap.name, target_path)
        if overwrites and not ui.confirm(
                "%s exists: %d file%s will be OVERWRITTEN with the snapshot "
                "version. Proceed? [y/N]"
                % (target_path, overwrites, "s" if overwrites > 1 else "")):
            self.note("restore cancelled")
            return
        ok, msg = ops.restore_snapshot(dest.job, snap.name, target_path)
        self.note(msg, _C_GREEN if ok else _C_RED)

    def do_verify(self, ui) -> None:
        dest = self.dest
        if dest is None or not dest.snapshots:
            self.note("nothing to verify", _C_YELLOW)
            return
        if self.col == 2 and self.snap is not None:
            snaps = [self.snap]
        else:
            snaps = dest.snapshots
        ui.busy("verifying %d snapshot%s…"
                % (len(snaps), "s" if len(snaps) > 1 else ""))
        tallies = {}
        detail = ""
        for s in snaps:
            verdict, msg = ops.verify_snapshot(self.conn, dest.job, s.name)
            s.verdict = verdict
            tallies[verdict] = tallies.get(verdict, 0) + 1
            if verdict == "suspect" and not detail:
                detail = " — %s: %s" % (s.name, msg)
        summary = ", ".join("%d %s" % (n, v) for v, n in sorted(tallies.items()))
        color = (_C_RED if "suspect" in tallies or "lost" in tallies
                 else _C_GREEN)
        self.note("verified: %s%s" % (summary, detail), color)


# ---------------------------------------------------------------- curses


def _pretty_path(path: str) -> str:
    home = str(Path.home())
    if path == home or path.startswith(home + "/"):
        return "~" + path[len(home):]
    return path


def _fit_path(path: str, width: int) -> str:
    """Truncate a path from the LEFT, keeping the distinctive tail."""
    if width <= 0:
        return ""
    if len(path) <= width:
        return path
    if width <= 1:
        return path[-width:]
    return "…" + path[-(width - 1):]


def _truncate(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[:width - 1] + "…"


class Ui:
    """Thin curses wrapper handed to App actions for prompts/confirms."""

    def __init__(self, scr, app: "App"):
        self.scr = scr
        self.app = app

    def _bar(self, text: str, color: int = _C_NORMAL) -> None:
        h, w = self.scr.getmaxyx()
        self.scr.move(h - 1, 0)
        self.scr.clrtoeol()
        self.scr.addnstr(h - 1, 0, _truncate(text, w - 1), w - 1,
                         curses.color_pair(color) | curses.A_BOLD)
        self.scr.refresh()

    def busy(self, text: str) -> None:
        self._bar(text, _C_YELLOW)

    def confirm(self, question: str) -> bool:
        self._bar(question, _C_YELLOW)
        return self.scr.getch() in (ord("y"), ord("Y"))

    def choose(self, question: str, keys: str) -> Optional[str]:
        self._bar(question, _C_YELLOW)
        while True:
            ch = self.scr.getch()
            if ch in (27, ord("q")):
                return None
            if 0 <= ch < 256 and chr(ch).lower() in keys:
                return chr(ch).lower()

    def prompt(self, label: str, initial: str) -> Optional[str]:
        buf = list(initial)
        curses.curs_set(1)
        try:
            while True:
                h, w = self.scr.getmaxyx()
                text = label + "".join(buf)
                self._bar(_truncate(text, w - 2), _C_NORMAL)
                self.scr.move(h - 1, min(len(text), w - 2))
                ch = self.scr.get_wch()
                if isinstance(ch, str):
                    if ch in ("\n", "\r"):
                        return "".join(buf)
                    if ch == "\x1b":                      # Esc
                        return None
                    if ch in ("\x7f", "\b"):
                        if buf:
                            buf.pop()
                    elif ch.isprintable():
                        buf.append(ch)
                elif ch in (curses.KEY_BACKSPACE,):
                    if buf:
                        buf.pop()
                elif ch == curses.KEY_ENTER:
                    return "".join(buf)
        finally:
            curses.curs_set(0)


def _init_colors() -> None:
    try:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(_C_SEL, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(_C_RED, curses.COLOR_RED, -1)
        curses.init_pair(_C_GREEN, curses.COLOR_GREEN, -1)
        curses.init_pair(_C_YELLOW, curses.COLOR_YELLOW, -1)
        curses.init_pair(_C_DIM, curses.COLOR_WHITE, -1)
        curses.init_pair(_C_SEL_RED, curses.COLOR_RED, curses.COLOR_CYAN)
    except curses.error:
        pass  # monochrome terminal: color_pair() degrades to plain attrs


def _scroll_start(sel: int, total: int, visible: int) -> int:
    """First row to draw so that the selected row stays on screen."""
    if total <= visible or visible <= 0:
        return 0
    return max(0, min(sel - visible + 1, total - visible))


def _draw_rows(win, rows, sel_row: int, max_row: int, inner: int) -> None:
    """Render `rows` (text, color|None, selected, bold) scrolled to sel_row.

    color None draws a section header (dim bold, never selected).
    """
    start = _scroll_start(sel_row, len(rows), max_row)
    for r, (text, color, selected, bold) in enumerate(
            rows[start:start + max_row]):
        if color is None:
            win.addnstr(r + 1, 1, _truncate(text, inner), inner,
                        curses.A_DIM | curses.A_BOLD)
        else:
            _put(win, r + 1, text, inner, color, selected=selected, bold=bold)
    if start > 0:
        win.addnstr(1, inner, "↑", 1, curses.A_DIM)
    if start + max_row < len(rows):
        win.addnstr(max_row, inner, "↓", 1, curses.A_DIM)


def _pane(scr, y, x, h, w, title: str, focused: bool):
    win = scr.derwin(h, w, y, x)
    win.box()
    attr = curses.A_BOLD if focused else curses.A_NORMAL
    win.addnstr(0, 2, _truncate(" %s " % title, w - 4), w - 4, attr)
    return win


def _put(win, row, text, width, color=_C_NORMAL, selected=False, bold=False):
    attr = curses.color_pair(_C_SEL if selected and color != _C_RED else
                             _C_SEL_RED if selected else color)
    if bold:
        attr |= curses.A_BOLD
    win.addnstr(row, 1, _truncate(text, width).ljust(width), width, attr)


def _draw(scr, app: App) -> None:
    scr.erase()
    h, w = scr.getmaxyx()
    if h < 8 or w < 60:
        scr.addnstr(0, 0, "terminal too small (need at least 60x8)", w - 1)
        scr.refresh()
        return
    body_h = h - 2
    w1 = max(20, int(w * 0.24))
    w2 = max(24, int(w * 0.40))
    w3 = w - w1 - w2

    tasks_win = _pane(scr, 0, 0, body_h, w1, "Tasks", app.col == 0)
    dests_win = _pane(scr, 0, w1, body_h, w2, "Destinations", app.col == 1)
    snaps_win = _pane(scr, 0, w1 + w2, body_h, w3, "Snapshots", app.col == 2)

    # ---- tasks, grouped into ACTIVE / ARCHIVED sections
    inner1 = w1 - 2
    max_row = body_h - 2
    sections = [("ACTIVE", [t for t in app.model.tasks if not t.archived]),
                ("ARCHIVED", [t for t in app.model.tasks if t.archived])]
    task_rows = []
    sel_row = 0
    for header, tasks in sections:
        task_rows.append((header, None, False, False))
        if not tasks:
            task_rows.append(("  (none)", _C_DIM, False, False))
        for t in tasks:
            i = app.model.tasks.index(t)
            selected = i == app.task_i
            if selected:
                sel_row = len(task_rows)
            tag = ""
            if t.archived:
                tag = " [LOST]" if t.lost else " [archived]"
            color = _C_RED if (t.archived and t.lost) else (
                _C_YELLOW if t.archived else _C_NORMAL)
            task_rows.append((" %s%s" % (t.label, tag), color, selected,
                              selected and app.col == 0))
    _draw_rows(tasks_win, task_rows, sel_row, max_row, inner1)

    # ---- destinations of the selected task
    inner2 = w2 - 2
    task = app.task
    if task is None:
        dests_win.addnstr(1, 1, _truncate("no jobs registered", inner2), inner2,
                          curses.A_DIM)
    else:
        dest_rows = []
        for i, d in enumerate(task.dests):
            present = sum(1 for s in d.snapshots if s.on_disk)
            lost = sum(1 for s in d.snapshots if s.lost)
            if task.archived and d.lost:
                bits = "ARCHIVE LOST"
            else:
                bits = "%d snap%s" % (present, "s" if present != 1 else "")
                if lost:
                    bits += ", %d LOST" % lost
            suffix = "  [%s]  %s" % (d.state, bits)
            path_w = max(8, inner2 - len(suffix) - 1)
            line = " %s%s" % (
                _fit_path(_pretty_path(d.job.dest), path_w).ljust(path_w),
                suffix)
            color = _C_RED if (lost or d.state == "blocked"
                               or (task.archived and d.lost)) \
                else _state_color(d.state)
            dest_rows.append((line, color, i == app.dest_i,
                              i == app.dest_i and app.col == 1))
        _draw_rows(dests_win, dest_rows, app.dest_i, max_row, inner2)

    # ---- snapshots of the selected destination
    inner3 = w3 - 2
    dest = app.dest
    if dest is None or not dest.snapshots:
        snaps_win.addnstr(1, 1, _truncate("no snapshots", inner3), inner3,
                          curses.A_DIM)
    else:
        snap_rows = []
        for i, s in enumerate(dest.snapshots):
            if s.lost:
                tag, color = "  LOST", _C_RED
            elif s.verdict == "suspect":
                tag, color = "  SUSPECT", _C_YELLOW
            elif s.verdict in ("ok", "baseline"):
                tag, color = "  ok", _C_GREEN
            else:
                tag, color = "", _C_NORMAL
            size = ("  %s" % ops.human_size(s.total_bytes)
                    if s.total_bytes is not None else "")
            line = " %s%s%s" % (s.name, size, tag)
            snap_rows.append((line, color, i == app.snap_i,
                              i == app.snap_i and app.col == 2))
        _draw_rows(snaps_win, snap_rows, app.snap_i, max_row, inner3)

    # ---- detail + status bars
    detail = ""
    if dest is not None:
        job = dest.job
        detail = "%s: %s -> %s | %s | keep %d | last: %s %s" % (
            job.name, _pretty_path(job.source), _pretty_path(job.dest),
            job.schedule_human, job.keep,
            job.last_run_at or "-", job.last_status or "")
        if job.blocked_reason:
            detail += " | blocked: %s" % job.blocked_reason
        if job.archived_at:
            detail += " | archived %s (%s)" % (
                job.archived_at, job.archived_reason or "manual")
    scr.addnstr(h - 2, 0, _truncate(detail, w - 1), w - 1, curses.A_DIM)
    status = app.message or _HELP
    scr.addnstr(h - 1, 0, _truncate(status, w - 1), w - 1,
                curses.color_pair(app.message_color) if app.message
                else curses.A_DIM)
    scr.refresh()


def _run(scr, conn) -> None:
    curses.curs_set(0)
    scr.keypad(True)
    _init_colors()
    app = App(conn)
    ui = Ui(scr, app)
    app.refresh_model(keep_selection=False)
    while True:
        _draw(scr, app)
        ch = scr.getch()
        if ch in (ord("q"), ord("Q")):
            break
        app.message = ""
        if ch in (curses.KEY_DOWN, ord("j")):
            app.move(1)
        elif ch in (curses.KEY_UP, ord("k")):
            app.move(-1)
        elif ch in (curses.KEY_LEFT, ord("h")):
            app.focus(app.col - 1)
        elif ch in (curses.KEY_RIGHT, ord("l"), curses.KEY_ENTER, 10, 13):
            app.focus(app.col + 1)
        elif ch == ord("\t"):
            app.focus((app.col + 1) % 3)
        elif ch in (ord("g"), curses.KEY_F5):
            app.refresh_model()
            if not app.message:
                app.note("refreshed")
        elif ch == ord("r"):
            app.do_restore(ui)
        elif ch == ord("v"):
            app.do_verify(ui)
        elif ch == ord("a"):
            app.do_archive(ui)
        elif ch == ord("i"):
            app.do_import(ui)
        elif ch == ord("u"):
            app.do_unarchive(ui)
        elif ch == ord("d"):
            app.do_delete(ui)
        elif ch == curses.KEY_RESIZE:
            pass


def main() -> int:
    conn = db.connect()
    try:
        curses.wrapper(_run, conn)
    finally:
        conn.close()
    return 0
