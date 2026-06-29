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


def test_add_rejects_keep_zero(xdg, tmp_path, monkeypatch, capsys):
    _silence_systemd(monkeypatch)
    src = tmp_path / "proj"; dst = tmp_path / "bak"
    src.mkdir(); dst.mkdir()
    rc = cli.main(["add", "--source", str(src), "--dest", str(dst),
                   "--schedule", "hourly", "--keep", "0"])
    assert rc != 0
    assert "keep" in capsys.readouterr().err.lower()


def test_edit_rejects_keep_zero(xdg, tmp_path, monkeypatch, capsys):
    _silence_systemd(monkeypatch)
    src = tmp_path / "proj"; dst = tmp_path / "bak"
    src.mkdir(); dst.mkdir()
    cli.main(["add", "--source", str(src), "--dest", str(dst), "--schedule", "hourly"])
    rc = cli.main(["edit", "proj", "--keep", "0"])
    assert rc != 0


def test_add_rolls_back_on_install_failure(xdg, tmp_path, monkeypatch):
    # systemctl returns non-zero -> install_units raises -> add must fail and not persist the job
    class _Fail:
        returncode = 1
        stdout = ""
        stderr = "boom"
    monkeypatch.setattr(units, "_systemctl", lambda *a: _Fail())
    monkeypatch.setattr(units, "is_active", lambda name: False)
    src = tmp_path / "proj"; dst = tmp_path / "bak"
    src.mkdir(); dst.mkdir()
    rc = cli.main(["add", "--source", str(src), "--dest", str(dst), "--schedule", "hourly"])
    assert rc != 0
    import backup.db as db
    conn = db.connect()
    assert db.get_job(conn, "proj") is None


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_edit_rename_moves_snapshot_tree(xdg, tmp_path, monkeypatch):
    _silence_systemd(monkeypatch)
    src = tmp_path / "proj"; dst = tmp_path / "bak"
    src.mkdir(); dst.mkdir()
    (src / "f.txt").write_text("hi")
    cli.main(["add", "--source", str(src), "--dest", str(dst), "--schedule", "hourly"])
    cli.main(["run", "proj"])
    assert (dst / "proj" / "snapshots").is_dir()
    assert cli.main(["edit", "proj", "--rename", "renamed"]) == 0
    assert not (dst / "proj").exists()
    assert (dst / "renamed" / "snapshots").is_dir()


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_edit_dest_change_refused_when_snapshots_exist(xdg, tmp_path, monkeypatch, capsys):
    _silence_systemd(monkeypatch)
    src = tmp_path / "proj"; dst = tmp_path / "bak"; dst2 = tmp_path / "bak2"
    src.mkdir(); dst.mkdir(); dst2.mkdir()
    (src / "f.txt").write_text("hi")
    cli.main(["add", "--source", str(src), "--dest", str(dst), "--schedule", "hourly"])
    cli.main(["run", "proj"])
    rc = cli.main(["edit", "proj", "--dest", str(dst2)])
    assert rc != 0
    assert "orphan" in capsys.readouterr().err.lower()


def test_edit_dest_change_allowed_without_snapshots(xdg, tmp_path, monkeypatch):
    _silence_systemd(monkeypatch)
    src = tmp_path / "proj"; dst = tmp_path / "bak"; dst2 = tmp_path / "bak2"
    src.mkdir(); dst.mkdir()
    cli.main(["add", "--source", str(src), "--dest", str(dst), "--schedule", "hourly"])
    assert cli.main(["edit", "proj", "--dest", str(dst2)]) == 0
    assert dst2.is_dir()


def test_config_sets_and_shows_default_dest(xdg, tmp_path, monkeypatch, capsys):
    import backup.db as db
    dst = tmp_path / "defaults"
    assert cli.main(["config", "--default-dest", str(dst)]) == 0
    assert dst.is_dir()
    capsys.readouterr()
    assert cli.main(["config"]) == 0
    out = capsys.readouterr().out
    assert str(dst) in out
    conn = db.connect()
    assert db.get_config(conn, "default_dest") == str(dst)


def test_config_show_when_unset(xdg, tmp_path, monkeypatch, capsys):
    assert cli.main(["config"]) == 0
    assert "not set" in capsys.readouterr().out.lower()


def test_add_uses_default_dest_when_dest_omitted(xdg, tmp_path, monkeypatch):
    import backup.db as db
    _silence_systemd(monkeypatch)
    src = tmp_path / "proj"
    dst = tmp_path / "defaults"
    src.mkdir()
    cli.main(["config", "--default-dest", str(dst)])
    assert cli.main(["add", "--source", str(src), "--schedule", "hourly"]) == 0
    conn = db.connect()
    assert db.get_job(conn, "proj").dest == str(dst)


def test_add_errors_when_no_dest_and_no_default(xdg, tmp_path, monkeypatch, capsys):
    _silence_systemd(monkeypatch)
    src = tmp_path / "proj"
    src.mkdir()
    rc = cli.main(["add", "--source", str(src), "--schedule", "hourly"])
    assert rc != 0
    assert "destination" in capsys.readouterr().err.lower()


def test_add_dest_overrides_default(xdg, tmp_path, monkeypatch):
    import backup.db as db
    _silence_systemd(monkeypatch)
    src = tmp_path / "proj"
    default_dst = tmp_path / "defaults"
    explicit_dst = tmp_path / "explicit"
    src.mkdir()
    cli.main(["config", "--default-dest", str(default_dst)])
    assert cli.main(["add", "--source", str(src), "--dest", str(explicit_dst),
                     "--schedule", "hourly"]) == 0
    conn = db.connect()
    assert db.get_job(conn, "proj").dest == str(explicit_dst)


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_run_all_runs_every_job(xdg, tmp_path, monkeypatch, capsys):
    _silence_systemd(monkeypatch)
    dst = tmp_path / "bak"
    dst.mkdir()
    for nm in ("alpha", "beta"):
        src = tmp_path / nm
        src.mkdir()
        (src / "f.txt").write_text(nm)
        cli.main(["add", "--source", str(src), "--dest", str(dst),
                  "--schedule", "hourly", "--name", nm])
    capsys.readouterr()
    rc = cli.main(["run", "--all"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "2 ok, 0 failed" in out
    assert (dst / "alpha" / "snapshots").is_dir()
    assert (dst / "beta" / "snapshots").is_dir()


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync required")
def test_run_all_continues_past_failure_and_exits_nonzero(xdg, tmp_path, monkeypatch, capsys):
    _silence_systemd(monkeypatch)
    dst = tmp_path / "bak"
    dst.mkdir()
    good = tmp_path / "good"
    good.mkdir()
    (good / "f.txt").write_text("ok")
    bad = tmp_path / "bad"
    bad.mkdir()
    cli.main(["add", "--source", str(good), "--dest", str(dst),
              "--schedule", "hourly", "--name", "good"])
    cli.main(["add", "--source", str(bad), "--dest", str(dst),
              "--schedule", "hourly", "--name", "bad"])
    shutil.rmtree(bad)  # make the 'bad' job fail at run time
    capsys.readouterr()
    rc = cli.main(["run", "--all"])
    out = capsys.readouterr().out
    assert rc != 0
    assert "1 ok, 1 failed" in out
    assert (dst / "good" / "snapshots").is_dir()  # healthy job still ran


def test_run_requires_name_or_all(xdg, tmp_path, monkeypatch, capsys):
    _silence_systemd(monkeypatch)
    rc = cli.main(["run"])
    assert rc != 0
    assert "--all" in capsys.readouterr().err


def test_run_name_and_all_conflict(xdg, tmp_path, monkeypatch, capsys):
    _silence_systemd(monkeypatch)
    rc = cli.main(["run", "proj", "--all"])
    assert rc != 0
