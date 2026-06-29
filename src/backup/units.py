from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional, Tuple

from . import paths

SERVICE_TEMPLATE = """\
[Unit]
Description=backup snapshot for {name}
After=network.target

[Service]
Type=oneshot
ExecStart={exec_path} _run {name}
WorkingDirectory={source}
"""

TIMER_TEMPLATE = """\
[Unit]
Description=backup timer for {name}

[Timer]
OnCalendar={oncalendar}
Persistent=true

[Install]
WantedBy=timers.target
"""


def _timer_unit(name: str) -> str:
    return "backup-%s.timer" % name


def _service_unit(name: str) -> str:
    return "backup-%s.service" % name


def unit_paths(name: str) -> Tuple[Path, Path]:
    d = paths.systemd_user_dir()
    return d / _service_unit(name), d / _timer_unit(name)


def render_service(name: str, exec_path: str, source: str) -> str:
    return SERVICE_TEMPLATE.format(
        name=name,
        exec_path=exec_path.replace("%", "%%"),
        source=source.replace("%", "%%"),
    )


def render_timer(name: str, oncalendar: str) -> str:
    return TIMER_TEMPLATE.format(name=name, oncalendar=oncalendar)


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True,
    )


def _systemctl_checked(*args: str) -> None:
    result = _systemctl(*args)
    if result.returncode != 0:
        raise RuntimeError(
            "systemctl --user %s failed (code %d): %s"
            % (" ".join(args), result.returncode, result.stderr.strip())
        )


def install_units(name: str, oncalendar: str, exec_path: str, source: str) -> None:
    paths.systemd_user_dir().mkdir(parents=True, exist_ok=True)
    svc, timer = unit_paths(name)
    svc.write_text(render_service(name, exec_path, source))
    timer.write_text(render_timer(name, oncalendar))
    _systemctl_checked("daemon-reload")
    _systemctl_checked("enable", "--now", _timer_unit(name))


def remove_units(name: str) -> None:
    _systemctl("disable", "--now", _timer_unit(name))
    svc, timer = unit_paths(name)
    for p in (svc, timer):
        if p.exists():
            p.unlink()
    _systemctl("daemon-reload")


def pause_units(name: str) -> None:
    _systemctl_checked("disable", "--now", _timer_unit(name))


def resume_units(name: str) -> None:
    _systemctl_checked("enable", "--now", _timer_unit(name))


def run_now(name: str) -> None:
    _systemctl("start", _service_unit(name))


def is_active(name: str) -> bool:
    return _systemctl("is-enabled", _timer_unit(name)).returncode == 0


def next_run(name: str) -> Optional[str]:
    result = _systemctl("list-timers", "--all", "--no-pager", _timer_unit(name))
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if _timer_unit(name) in line:
            return line.strip()
    return None
