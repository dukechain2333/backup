from __future__ import annotations

import importlib
import pytest


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    """Point all XDG dirs at a tmp dir and return the config/state roots."""
    cfg = tmp_path / "config"
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    import backup.paths as paths
    importlib.reload(paths)
    paths.ensure_dirs()
    return {"config": cfg / "backup", "state": state / "backup", "paths": paths}
