from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from . import db, paths, runner, schedule, units


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return slug or "job"


def _err(msg: str) -> int:
    print("error: %s" % msg, file=sys.stderr)
    return 1


def _resolve(p: str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(p)))


def _is_inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _confirm_duplicate_source(source, dest, existing, assume_yes: bool) -> bool:
    """Warn that `source` is already backed up elsewhere; return True to proceed."""
    sys.stderr.write("note: %s is already backed up:\n" % source)
    for job in existing:
        sys.stderr.write("  - job %r -> %s\n" % (job.name, job.dest))
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        sys.stderr.write(
            "error: source already registered; re-run with --yes to add "
            "another destination\n")
        return False
    reply = input("Add another backup of this source to %s? [y/N] " % dest)
    if reply.strip().lower() in ("y", "yes"):
        return True
    print("aborted.")
    return False


def cmd_add(args) -> int:
    source = _resolve(args.source or os.getcwd())
    if not source.is_dir():
        return _err("source is not a directory: %s" % source)

    conn = db.connect()
    dest_arg = args.dest or db.get_config(conn, "default_dest")
    if not dest_arg:
        return _err("no destination: pass --dest or set one with "
                    "'backup config --default-dest <path>'")
    dest = _resolve(dest_arg)
    if _is_inside(dest, source) or dest == source:
        return _err("destination %s is inside source %s (would recurse)"
                    % (dest, source))

    name = args.name or slugify(source.name)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        return _err("invalid name %r (use lowercase letters, digits, hyphens)" % name)
    if args.keep < 1:
        return _err("--keep must be at least 1")
    if any(ord(c) < 32 for c in str(source)):
        return _err("source path contains control characters")

    try:
        sched = schedule.parse_schedule(args.schedule)
    except ValueError as exc:
        return _err(str(exc))
    if not schedule.validate_oncalendar(sched.oncalendar):
        return _err("systemd rejected schedule: %s" % sched.oncalendar)

    clash = db.get_job(conn, name)
    if clash is not None:
        hint = (" (pass --name to add another backup of the same source)"
                if clash.source == str(source) else "")
        return _err("a job named %r already exists%s" % (name, hint))
    same_source = db.list_jobs_by_source(conn, str(source))
    dup = next((j for j in same_source if j.dest == str(dest)), None)
    if dup is not None:
        return _err("source already backed up to %s as job %r" % (dest, dup.name))
    if same_source and not _confirm_duplicate_source(source, dest, same_source, args.yes):
        return 1

    dest.mkdir(parents=True, exist_ok=True)
    job = db.Job(
        name=name, source=str(source), dest=str(dest),
        oncalendar=sched.oncalendar, schedule_human=sched.human,
        keep=args.keep, created_at=datetime.now().isoformat(timespec="seconds"),
        job_id=uuid.uuid4().hex,
    )
    db.add_job(conn, job)
    try:
        units.install_units(name, sched.oncalendar, paths.backup_executable(), str(source))
    except RuntimeError as exc:
        db.remove_job(conn, name)
        return _err("failed to install timer: %s" % exc)
    print("added job %r: %s -> %s (%s, keep %d)"
          % (name, source, dest, sched.human, args.keep))
    return 0


def cmd_config(args) -> int:
    conn = db.connect()
    if args.default_dest is not None:
        dest = _resolve(args.default_dest)
        dest.mkdir(parents=True, exist_ok=True)
        db.set_config(conn, "default_dest", str(dest))
        print("default-dest: %s" % dest)
        return 0
    current = db.get_config(conn, "default_dest")
    print("default-dest: %s" % (current if current else "(not set)"))
    return 0


def _require_job(conn, name: str):
    job = db.get_job(conn, name)
    if job is None:
        print("error: no job named %r" % name, file=sys.stderr)
    return job


def cmd_list(args) -> int:
    conn = db.connect()
    jobs = db.list_jobs(conn)
    if not jobs:
        print("no backup jobs registered. run 'backup add --dest <path>', or set a "
              "default with 'backup config --default-dest <path>' then 'backup add'.")
        return 0
    header = "%-14s %-8s %-18s %-20s %s" % (
        "NAME", "STATE", "SCHEDULE", "LAST RUN", "SOURCE -> DEST")
    print(header)
    for job in jobs:
        if job.blocked_reason:
            state = "blocked"
        else:
            state = "active" if units.is_active(job.name) else "paused"
        last = "%s %s" % (job.last_run_at or "-", job.last_status or "")
        print("%-14s %-8s %-18s %-20s %s -> %s" % (
            job.name, state, job.schedule_human, last.strip(),
            job.source, job.dest))
    return 0


def cmd_status(args) -> int:
    conn = db.connect()
    job = _require_job(conn, args.name)
    if job is None:
        return 1
    if job.blocked_reason:
        state = "blocked"
    else:
        state = "active" if units.is_active(job.name) else "paused"
    print("job:       %s" % job.name)
    print("source:    %s" % job.source)
    print("dest:      %s" % job.dest)
    print("schedule:  %s (%s)" % (job.schedule_human, job.oncalendar))
    print("retention: keep %d snapshots" % job.keep)
    print("state:     %s" % state)
    if job.blocked_reason:
        print("blocked:   %s" % job.blocked_reason)
    print("last run:  %s [%s] %s" % (
        job.last_run_at or "-", job.last_status or "-", job.last_message or ""))
    nxt = units.next_run(job.name)
    if nxt:
        print("next:      %s" % nxt)
    logfile = paths.log_dir() / ("%s.log" % job.name)
    if logfile.exists():
        tail = logfile.read_text().splitlines()[-5:]
        print("recent log:")
        for line in tail:
            print("  %s" % line)
    return 0


def cmd_remove(args) -> int:
    conn = db.connect()
    job = _require_job(conn, args.name)
    if job is None:
        return 1
    units.remove_units(job.name)
    db.remove_job(conn, job.name)
    if args.purge:
        shutil.rmtree(runner.job_dir(job), ignore_errors=True)
        print("removed job %r and purged snapshots" % job.name)
    else:
        print("removed job %r (snapshots kept at %s)"
              % (job.name, runner.job_dir(job)))
    return 0


def cmd_pause(args) -> int:
    conn = db.connect()
    job = _require_job(conn, args.name)
    if job is None:
        return 1
    try:
        units.pause_units(job.name)
    except RuntimeError as exc:
        return _err(str(exc))
    print("paused %r" % job.name)
    return 0


def cmd_resume(args) -> int:
    conn = db.connect()
    job = _require_job(conn, args.name)
    if job is None:
        return 1
    try:
        units.resume_units(job.name)
    except RuntimeError as exc:
        return _err(str(exc))
    print("resumed %r" % job.name)
    return 0


def cmd_run(args) -> int:
    conn = db.connect()
    if args.all and args.name:
        return _err("give a job name or --all, not both")
    if not args.all and not args.name:
        return _err("specify a job name or --all")

    if args.all:
        jobs = db.list_jobs(conn)
        if not jobs:
            print("no backup jobs registered.")
            return 0
        ok = 0
        failed = 0
        for job in jobs:
            result = runner.run_backup(job, conn=conn, force=args.force)
            print("%s: %s: %s" % (job.name, result.status, result.message))
            if result.status == "ok":
                ok += 1
            else:
                failed += 1
        print("%d ok, %d failed" % (ok, failed))
        return 0 if failed == 0 else 1

    job = _require_job(conn, args.name)
    if job is None:
        return 1
    result = runner.run_backup(job, conn=conn, force=args.force)
    print("%s: %s" % (result.status, result.message))
    return 0 if result.status == "ok" else 1


def cmd_logs(args) -> int:
    conn = db.connect()
    job = _require_job(conn, args.name)
    if job is None:
        return 1
    logfile = paths.log_dir() / ("%s.log" % job.name)
    if not logfile.exists():
        print("no log yet for %r" % job.name)
        return 0
    lines = logfile.read_text().splitlines()
    for line in lines[-args.lines:]:
        print(line)
    return 0


def cmd_internal_run(args) -> int:
    conn = db.connect()
    job = db.get_job(conn, args.name)
    if job is None:
        return _err("no job named %r" % args.name)
    result = runner.run_backup(job, conn=conn)
    return 0 if result.status == "ok" else 1


def cmd_edit(args) -> int:
    conn = db.connect()
    job = _require_job(conn, args.name)
    if job is None:
        return 1
    updates = {}
    oncalendar = job.oncalendar
    if args.schedule:
        try:
            sched = schedule.parse_schedule(args.schedule)
        except ValueError as exc:
            return _err(str(exc))
        if not schedule.validate_oncalendar(sched.oncalendar):
            return _err("systemd rejected schedule: %s" % sched.oncalendar)
        updates["oncalendar"] = sched.oncalendar
        updates["schedule_human"] = sched.human
        oncalendar = sched.oncalendar
    if args.keep is not None:
        if args.keep < 1:
            return _err("--keep must be at least 1")
        updates["keep"] = args.keep

    has_snapshots = bool(runner.list_snapshots(job))

    if args.dest:
        new_dest = _resolve(args.dest)
        if _is_inside(new_dest, Path(job.source)) or new_dest == Path(job.source):
            return _err("destination %s is inside source %s (would recurse)"
                        % (new_dest, job.source))
        if str(new_dest) != job.dest and has_snapshots:
            return _err(
                "job %r has existing snapshots at %s; changing --dest would orphan "
                "them. Remove and re-add the job at the new destination, or move that "
                "directory manually." % (job.name, runner.job_dir(job)))
        new_dest.mkdir(parents=True, exist_ok=True)
        updates["dest"] = str(new_dest)

    if args.rename:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", args.rename):
            return _err("invalid name %r" % args.rename)
        if db.get_job(conn, args.rename) is not None:
            return _err("a job named %r already exists" % args.rename)

    old_job_dir = runner.job_dir(job)

    new_name = args.rename or job.name
    if args.rename:
        units.remove_units(job.name)
        updates["name"] = args.rename
    db.update_job(conn, job.name, **updates)
    updated = db.get_job(conn, new_name)

    new_job_dir = runner.job_dir(updated)
    if old_job_dir != new_job_dir and old_job_dir.exists():
        new_job_dir.parent.mkdir(parents=True, exist_ok=True)
        os.rename(old_job_dir, new_job_dir)

    if args.schedule or args.rename:
        try:
            units.install_units(updated.name, oncalendar,
                                paths.backup_executable(), updated.source)
        except RuntimeError as exc:
            return _err("failed to update timer: %s" % exc)
    print("updated %r" % new_name)
    return 0


def cmd_snapshots(args) -> int:
    conn = db.connect()
    job = _require_job(conn, args.name)
    if job is None:
        return 1
    snaps = runner.list_snapshots(job)
    if not snaps:
        print("no snapshots yet for %r" % job.name)
        return 0
    for snap in reversed(snaps):
        size = _dir_size(snap)
        print("%-22s %s" % (snap.name, _human(size)))
    return 0


def cmd_preview(args) -> int:
    conn = db.connect()
    job = _require_job(conn, args.name)
    if job is None:
        return 1
    if not Path(job.source).is_dir():
        return _err("source is not a directory: %s" % job.source)
    files = runner.preview_backup(job)
    if not files:
        print("nothing to back up")
        return 0
    for path in files:
        print(path)
    return 0


def cmd_restore(args) -> int:
    conn = db.connect()
    job = _require_job(conn, args.name)
    if job is None:
        return 1
    snaps = runner.list_snapshots(job)
    if not snaps:
        return _err("no snapshots to restore for %r" % job.name)
    if args.snapshot:
        chosen = next((s for s in snaps if s.name == args.snapshot), None)
        if chosen is None:
            return _err("snapshot %r not found" % args.snapshot)
    else:
        chosen = snaps[-1]
    target = _resolve(args.to) if args.to else (
        Path(job.source).parent / ("restore-%s" % chosen.name))
    result = subprocess.run(["rsync", "-a", "%s/" % chosen, "%s/" % target], check=False)
    if result.returncode not in (0, 24):
        return _err("rsync failed (code %d)" % result.returncode)
    print("restored %s -> %s" % (chosen.name, target))
    return 0


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            fp = Path(root) / f
            try:
                total += fp.stat().st_size
            except OSError:
                pass
    return total


def _human(n: int) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024:
            return "%d%s" % (n, unit)
        n //= 1024
    return "%dP" % n


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="backup", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("add", help="register the current dir as a backup job")
    a.add_argument("--source", help="directory to back up (default: cwd)")
    a.add_argument("--dest",
                   help="local destination directory (default: configured default-dest)")
    a.add_argument("--schedule", default="daily@02:00",
                   help="hourly | daily@HH:MM | weekly@dow:HH:MM | every:Nh | every:Nm")
    a.add_argument("--keep", type=int, default=7, help="snapshots to retain")
    a.add_argument("--name", help="job name (default: source basename)")
    a.add_argument("--yes", action="store_true",
                   help="skip the confirmation when adding another destination "
                        "for an already-backed-up source")
    a.set_defaults(func=cmd_add)

    c = sub.add_parser("config", help="show or set configuration")
    c.add_argument("--default-dest", dest="default_dest",
                   help="set the default destination used by 'add' when --dest is omitted")
    c.set_defaults(func=cmd_config)

    sub.add_parser("list", help="list jobs").set_defaults(func=cmd_list)

    for cmd, fn, help_ in [
        ("status", cmd_status, "show job detail"),
        ("pause", cmd_pause, "pause a job's timer"),
        ("resume", cmd_resume, "resume a job's timer"),
        ("snapshots", cmd_snapshots, "list snapshots for a job"),
        ("preview", cmd_preview, "list files that would be backed up (.backupignore applied)"),
    ]:
        sp = sub.add_parser(cmd, help=help_)
        sp.add_argument("name")
        sp.set_defaults(func=fn)

    rn = sub.add_parser("run", help="run a backup now (one job, or --all)")
    rn.add_argument("name", nargs="?", help="job to run (omit with --all)")
    rn.add_argument("--all", action="store_true", help="run every job")
    rn.add_argument("--force", action="store_true",
                    help="skip integrity check, clear blocked, and re-baseline")
    rn.set_defaults(func=cmd_run)

    lg = sub.add_parser("logs", help="show a job's log")
    lg.add_argument("name")
    lg.add_argument("--lines", type=int, default=40, help="lines to show (default 40)")
    lg.set_defaults(func=cmd_logs)

    r = sub.add_parser("remove", help="delete a job")
    r.add_argument("name")
    r.add_argument("--purge", action="store_true", help="also delete snapshots")
    r.set_defaults(func=cmd_remove)

    e = sub.add_parser("edit", help="modify a job")
    e.add_argument("name")
    e.add_argument("--schedule")
    e.add_argument("--keep", type=int)
    e.add_argument("--dest")
    e.add_argument("--rename")
    e.set_defaults(func=cmd_edit)

    rs = sub.add_parser("restore", help="restore a snapshot")
    rs.add_argument("name")
    rs.add_argument("--snapshot", help="timestamp dir name (default: newest)")
    rs.add_argument("--to", help="destination dir (default: restore-<ts> by source)")
    rs.set_defaults(func=cmd_restore)

    ir = sub.add_parser("_run", help=argparse.SUPPRESS)
    ir.add_argument("name")
    ir.set_defaults(func=cmd_internal_run)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
