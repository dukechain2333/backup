from __future__ import annotations

import shutil

import pytest

import backup.cli as cli
import backup.units as units


def _silence_systemd(monkeypatch):
    monkeypatch.setattr(units, "_systemctl", lambda *a: _CP())
    monkeypatch.setattr(units, "is_active", lambda name: True)
    monkeypatch.setattr(units, "next_run", lambda name: None)


class _CP:
    returncode = 0
    stdout = ""


def test_slugify():
    assert cli.slugify("My Docs!") == "my-docs"


def test_add_creates_job_and_units(xdg, tmp_path, monkeypatch, capsys):
    _silence_systemd(monkeypatch)
    src = tmp_path / "proj"
    dst = tmp_path / "bak"
    src.mkdir()
    dst.mkdir()
    rc = cli.main(["add", "--source", str(src), "--dest", str(dst),
                   "--schedule", "daily@02:00"])
    assert rc == 0
    svc, timer = units.unit_paths("proj")
    assert svc.exists() and timer.exists()


def test_add_rejects_dest_inside_source(xdg, tmp_path, monkeypatch, capsys):
    _silence_systemd(monkeypatch)
    src = tmp_path / "proj"
    src.mkdir()
    inside = src / "backups"
    rc = cli.main(["add", "--source", str(src), "--dest", str(inside),
                   "--schedule", "hourly"])
    assert rc != 0
    assert "inside" in capsys.readouterr().err.lower()


def test_add_rejects_duplicate_source(xdg, tmp_path, monkeypatch, capsys):
    _silence_systemd(monkeypatch)
    src = tmp_path / "proj"
    dst = tmp_path / "bak"
    src.mkdir()
    dst.mkdir()
    assert cli.main(["add", "--source", str(src), "--dest", str(dst),
                     "--schedule", "hourly"]) == 0
    assert cli.main(["add", "--source", str(src), "--dest", str(dst),
                     "--schedule", "hourly", "--name", "other"]) != 0
    assert "already registered" in capsys.readouterr().err.lower()


def test_list_and_remove(xdg, tmp_path, monkeypatch, capsys):
    _silence_systemd(monkeypatch)
    src = tmp_path / "proj"
    dst = tmp_path / "bak"
    src.mkdir()
    dst.mkdir()
    cli.main(["add", "--source", str(src), "--dest", str(dst), "--schedule", "hourly"])
    assert cli.main(["list"]) == 0
    assert "proj" in capsys.readouterr().out
    assert cli.main(["remove", "proj"]) == 0
    assert cli.main(["remove", "proj"]) != 0  # already gone


def test_edit_rejects_dest_inside_source(xdg, tmp_path, monkeypatch, capsys):
    _silence_systemd(monkeypatch)
    src = tmp_path / "proj"
    dst = tmp_path / "bak"
    src.mkdir()
    dst.mkdir()
    cli.main(["add", "--source", str(src), "--dest", str(dst), "--schedule", "hourly"])
    rc = cli.main(["edit", "proj", "--dest", str(src / "inner")])
    assert rc != 0
    assert "inside" in capsys.readouterr().err.lower()


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_restore_reports_rsync_failure(xdg, tmp_path, monkeypatch, capsys):
    _silence_systemd(monkeypatch)
    src = tmp_path / "proj"
    dst = tmp_path / "bak"
    src.mkdir()
    dst.mkdir()
    (src / "f.txt").write_text("hi")
    cli.main(["add", "--source", str(src), "--dest", str(dst), "--schedule", "hourly"])
    cli.main(["run", "proj"])
    rc = cli.main(["restore", "proj", "--to", str(tmp_path / "nope" / "x" / "y")])
    assert rc != 0
