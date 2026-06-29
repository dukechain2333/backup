from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _xdg(env: str, default: Path) -> Path:
    value = os.environ.get(env)
    return Path(value) if value else default


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", Path.home() / ".config") / "backup"


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", Path.home() / ".local" / "state") / "backup"


def log_dir() -> Path:
    return state_dir() / "logs"


def db_path() -> Path:
    return config_dir() / "jobs.db"


def systemd_user_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", Path.home() / ".config") / "systemd" / "user"


def ensure_dirs() -> None:
    for d in (config_dir(), state_dir(), log_dir(), systemd_user_dir()):
        d.mkdir(parents=True, exist_ok=True)


def backup_executable() -> str:
    found = shutil.which("backup")
    if found:
        return str(Path(found).resolve())
    return str(Path(sys.argv[0]).resolve())
