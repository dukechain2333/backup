from __future__ import annotations

from pathlib import Path


def test_dirs_honor_xdg(xdg):
    paths = xdg["paths"]
    assert paths.config_dir() == xdg["config"]
    assert paths.state_dir() == xdg["state"]
    assert paths.log_dir() == xdg["state"] / "logs"
    assert paths.db_path() == xdg["config"] / "jobs.db"
    assert paths.systemd_user_dir().name == "user"


def test_ensure_dirs_creates_everything(xdg):
    paths = xdg["paths"]
    assert paths.config_dir().is_dir()
    assert paths.log_dir().is_dir()
    assert paths.systemd_user_dir().is_dir()


def test_backup_executable_is_absolute(xdg):
    exe = xdg["paths"].backup_executable()
    assert Path(exe).is_absolute()
