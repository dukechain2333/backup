from __future__ import annotations

import backup.units as units


def test_render_service_contains_run_command(xdg):
    text = units.render_service("docs", "/home/u/.local/bin/backup", "/a/docs")
    assert "ExecStart=/home/u/.local/bin/backup _run docs" in text
    assert "Type=oneshot" in text


def test_render_timer_contains_oncalendar(xdg):
    text = units.render_timer("docs", "*-*-* 02:00:00")
    assert "OnCalendar=*-*-* 02:00:00" in text
    assert "Persistent=true" in text


def test_install_units_writes_files_and_calls_systemctl(xdg, monkeypatch):
    calls = []
    monkeypatch.setattr(units, "_systemctl", lambda *a: calls.append(a) or _ok())
    units.install_units("docs", "*-*-* 02:00:00", "/bin/backup", "/a/docs")
    svc, timer = units.unit_paths("docs")
    assert svc.exists() and timer.exists()
    assert ("daemon-reload",) in calls
    assert ("enable", "--now", "backup-docs.timer") in calls


def test_remove_units_deletes_files(xdg, monkeypatch):
    monkeypatch.setattr(units, "_systemctl", lambda *a: _ok())
    units.install_units("docs", "*-*-* 02:00:00", "/bin/backup", "/a/docs")
    units.remove_units("docs")
    svc, timer = units.unit_paths("docs")
    assert not svc.exists() and not timer.exists()


class _CP:
    def __init__(self, rc):
        self.returncode = rc
        self.stdout = ""


def _ok():
    return _CP(0)
