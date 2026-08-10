from __future__ import annotations

from pathlib import Path

import backup.units as units
from backup import tui
from backup.db import add_job, connect, record_snapshot
from backup.runner import job_dir

from test_ops import _fake_snapshot, _silence_systemd, make_job


def test_load_model_groups_and_flags(tmp_path, monkeypatch):
    _silence_systemd(monkeypatch)
    monkeypatch.setattr(units, "remove_units", lambda name: None)
    conn = connect(tmp_path / "jobs.db")
    j1 = make_job(tmp_path, name="docs", dest="d1")
    j2 = make_job(tmp_path, name="docs-usb", dest="d2")
    add_job(conn, j1)
    add_job(conn, j2)
    _fake_snapshot(j1, "2026-08-08_02-00-00")
    record_snapshot(conn, "docs", "2026-08-08_02-00-00")
    record_snapshot(conn, "docs", "2026-08-09_02-00-00")  # lost

    model = tui.load_model(conn)
    assert len(model.tasks) == 1
    task = model.tasks[0]
    assert not task.archived
    assert [d.job.name for d in task.dests] == ["docs", "docs-usb"]
    d1 = task.dests[0]
    assert d1.state == "active"
    assert [s.name for s in d1.snapshots] == [
        "2026-08-09_02-00-00", "2026-08-08_02-00-00"]
    assert d1.snapshots[0].lost is True
    assert d1.snapshots[1].lost is False
    assert task.dests[1].lost is True  # d2 has no snapshot tree at all


def test_load_model_auto_archives_missing_source(tmp_path, monkeypatch):
    _silence_systemd(monkeypatch)
    monkeypatch.setattr(units, "remove_units", lambda name: None)
    conn = connect(tmp_path / "jobs.db")
    job = make_job(tmp_path, mkdirs=False)
    job.source = str(tmp_path / "vanished")
    add_job(conn, job)
    model = tui.load_model(conn)
    task = model.tasks[0]
    assert task.archived is True
    assert task.dests[0].state == "archived"
    assert task.lost is True  # no snapshots anywhere -> archive lost


class FakeUi:
    """Stands in for the curses Ui: canned answers, no terminal needed."""

    def __init__(self, confirm=True, choice=None, text=None):
        self._confirm = confirm
        self._choice = choice
        self._text = text
        self.confirmations = []

    def busy(self, text):
        pass

    def confirm(self, question):
        self.confirmations.append(question)
        return self._confirm

    def choose(self, question, keys):
        return self._choice

    def prompt(self, label, initial):
        return initial if self._text is None else self._text


def _app(conn):
    app = tui.App(conn)
    app.refresh_model(keep_selection=False)
    return app


def test_app_archive_and_delete_flow(tmp_path, monkeypatch):
    _silence_systemd(monkeypatch)
    monkeypatch.setattr(units, "remove_units", lambda name: None)
    conn = connect(tmp_path / "jobs.db")
    job = make_job(tmp_path)
    add_job(conn, job)
    snap = _fake_snapshot(job, "2026-08-08_02-00-00")

    app = _app(conn)
    app.do_delete(FakeUi(choice="k"))          # not archived yet -> refused
    assert len(app.model.tasks) == 1
    assert "archive" in app.message

    app.do_archive(FakeUi(confirm=True))
    assert app.model.tasks[0].archived is True
    assert snap.is_dir()                        # snapshots untouched

    app.do_delete(FakeUi(choice="k"))           # delete, keep snapshots
    assert app.model.tasks == []
    assert snap.is_dir()


def test_app_restore_uses_selected_snapshot(tmp_path, monkeypatch):
    _silence_systemd(monkeypatch)
    monkeypatch.setattr(units, "remove_units", lambda name: None)
    conn = connect(tmp_path / "jobs.db")
    job = make_job(tmp_path)
    add_job(conn, job)
    _fake_snapshot(job, "2026-08-08_02-00-00")
    record_snapshot(conn, "docs", "2026-08-09_02-00-00")  # lost

    import shutil as _shutil
    if _shutil.which("rsync") is None:
        return
    app = _app(conn)
    app.col = 2
    app.snap_i = 0                              # newest = the lost one
    app.do_restore(FakeUi(text=str(tmp_path / "out")))
    assert "LOST" in app.message                # refused
    assert not (tmp_path / "out").exists()

    app.snap_i = 1                              # the on-disk snapshot
    app.do_restore(FakeUi(text=str(tmp_path / "out")))
    assert (tmp_path / "out" / "a.txt").read_text() == "x"


def test_app_task_restore_picks_newest_across_dests(tmp_path, monkeypatch):
    import shutil as _shutil
    if _shutil.which("rsync") is None:
        return
    _silence_systemd(monkeypatch)
    monkeypatch.setattr(units, "remove_units", lambda name: None)
    conn = connect(tmp_path / "jobs.db")
    j1 = make_job(tmp_path, name="docs", dest="d1")
    j2 = make_job(tmp_path, name="docs-usb", dest="d2")
    add_job(conn, j1)
    add_job(conn, j2)
    _fake_snapshot(j1, "2026-01-01_00-00-00")   # first-listed dest: old snap
    old = job_dir(j2) / "snapshots" / "2026-08-01_00-00-00"
    old.mkdir(parents=True)
    (old / "a.txt").write_text("newer")

    app = _app(conn)
    app.col = 0                                  # task focused
    app.do_restore(FakeUi(text=str(tmp_path / "out")))
    # must restore d2's 2026-08-01 snapshot, not d1's 2026-01-01
    assert "2026-08-01_00-00-00" in app.message
    assert (tmp_path / "out" / "a.txt").read_text() == "newer"


def test_app_restore_into_nonempty_target_needs_confirmation(tmp_path, monkeypatch):
    import shutil as _shutil
    if _shutil.which("rsync") is None:
        return
    _silence_systemd(monkeypatch)
    monkeypatch.setattr(units, "remove_units", lambda name: None)
    conn = connect(tmp_path / "jobs.db")
    job = make_job(tmp_path)
    add_job(conn, job)
    _fake_snapshot(job, "2026-08-08_02-00-00")
    target = tmp_path / "out"
    target.mkdir()
    (target / "a.txt").write_text("current-version")

    app = _app(conn)
    app.col = 2
    app.snap_i = 0
    ui = FakeUi(confirm=False, text=str(target))     # user declines overwrite
    app.do_restore(ui)
    assert len(ui.confirmations) == 1
    assert "OVERWRITTEN" in ui.confirmations[0]
    assert (target / "a.txt").read_text() == "current-version"  # untouched

    app.do_restore(FakeUi(confirm=True, text=str(target)))      # user accepts
    assert (target / "a.txt").read_text() == "x"

    # fresh target: no files clash, so no confirmation step at all
    ui2 = FakeUi(confirm=False, text=str(tmp_path / "fresh"))
    app.do_restore(ui2)
    assert ui2.confirmations == []
    assert (tmp_path / "fresh" / "a.txt").read_text() == "x"


def test_verify_marks_suspect_snapshot(tmp_path, monkeypatch):
    _silence_systemd(monkeypatch)
    monkeypatch.setattr(units, "remove_units", lambda name: None)
    conn = connect(tmp_path / "jobs.db")
    job = make_job(tmp_path)
    add_job(conn, job)
    snap = _fake_snapshot(job, "2026-08-08_02-00-00")

    app = _app(conn)
    app.col = 2
    app.do_verify(FakeUi())                    # adopts baseline
    (snap / "a.txt").unlink()                  # corrupt it
    app.do_verify(FakeUi())
    assert app.dest.snapshots[0].verdict == "suspect"
    assert "suspect" in app.message


def test_scroll_start_keeps_selection_visible():
    assert tui._scroll_start(0, 30, 10) == 0
    assert tui._scroll_start(9, 30, 10) == 0
    assert tui._scroll_start(10, 30, 10) == 1
    assert tui._scroll_start(29, 30, 10) == 20
    assert tui._scroll_start(5, 5, 10) == 0      # fits entirely


def test_archived_dest_with_snapshots_not_lost(tmp_path, monkeypatch):
    _silence_systemd(monkeypatch)
    monkeypatch.setattr(units, "remove_units", lambda name: None)
    conn = connect(tmp_path / "jobs.db")
    job = make_job(tmp_path, mkdirs=False)
    Path(job.dest).mkdir(parents=True, exist_ok=True)
    job.source = str(tmp_path / "vanished")
    add_job(conn, job)
    _fake_snapshot(job, "2026-08-01_02-00-00")
    record_snapshot(conn, "docs", "2026-08-01_02-00-00")
    model = tui.load_model(conn)
    task = model.tasks[0]
    assert task.archived is True
    assert task.lost is False
    assert task.dests[0].snapshots[0].lost is False
