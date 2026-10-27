#!/usr/bin/env python3
"""Orphaned agent processes: find and report them (harmonic-forge#1181 AC1-AC3).
Deterministic and LLM-free. Report-only: nothing is ever signalled.

A candidate is a process of this user that is
  (a) reparented to the systemd user manager,
  (b) of agent origin: its cmdline contains ``/.claude/shell-snapshots/``, or
      its cwd is under ``~/.cache/hrse-scratch`` or ``~/Harmonic_Projects/.worktrees``,
  (c) and not the scratch-lock holder or a descendant of it, not a descendant
      of a live ``mise run check``, and not a member of an active
      ``--user`` ``.service`` unit (HRSE2's ``pc-up`` runs process-compose under
      ``systemd-run --user``, parented to the user manager by design).

An orphan is a candidate older than ``--age-hours`` that is idle (lifetime and
a 60 s sample both under 1% CPU) or runaway (both over 50%). One shared sample
covers every candidate in a run.

Each orphan is reported as one telemetry event (never its cmdline), and a run
that found any sends one ntfy summary.

The AC4 auto-stop was deleted on the operator's ruling after a sticky-wicket
REFORGE (comment 6440699722): /proc identity cannot be checked and signalled
atomically. A future kill path stops a process's own systemd scope as a unit
(AC7), recorded at spawn, never inferred from /proc.

Process facts come from ``/proc`` only. ``proc_root`` and the clock are
injectable, so the tests run on fixture trees.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
SCRATCH_LOCK = Path.home() / ".cache" / "hrse-scratch" / ".lock"
ORIGIN_DIRS = (Path.home() / ".cache" / "hrse-scratch", Path.home() / "Harmonic_Projects" / ".worktrees")
SNAPSHOT_MARK = "/.claude/shell-snapshots/"
IDLE_PCT, RUNAWAY_PCT = 1.0, 50.0
NTFY_TOPIC_FILE_ENV = "NTFY_TOPIC_FILE"


@dataclass
class Proc:
    pid: int
    ppid: int
    uid: int
    comm: str
    cmdline: str
    cwd: str
    start_ticks: int
    cpu_ticks: int
    cgroup: str
    session: int = 0
    argv: list[str] = field(default_factory=list)
    children: list[int] = field(default_factory=list)


def _read(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return None


def read_proc(proc_root: Path, pid: int) -> Proc | None:
    base = proc_root / str(pid)
    stat, status = _read(base / "stat"), _read(base / "status")
    if not stat or not status:
        return None
    try:
        # comm is parenthesised and may hold spaces: split after the last ")".
        rest = stat[stat.rindex(")") + 2:].split()
        ppid, session = int(rest[1]), int(rest[3])
        utime, stime, start = int(rest[11]), int(rest[12]), int(rest[19])
        uid = int(next(line.split()[1] for line in status.splitlines() if line.startswith("Uid:")))
    except (ValueError, IndexError, StopIteration):
        return None
    raw = _read(base / "cmdline") or ""
    argv = [a for a in raw.split("\0") if a]  # NUL-separated: keeps argv boundaries
    cmdline = " ".join(argv)
    try:
        cwd = os.readlink(base / "cwd")
    except OSError:
        cwd = ""
    return Proc(pid=pid, ppid=ppid, uid=uid, comm=(_read(base / "comm") or "").strip(),
                cmdline=cmdline, cwd=cwd, start_ticks=start, cpu_ticks=utime + stime,
                cgroup=(_read(base / "cgroup") or "").strip(), session=session, argv=argv)


def processes(proc_root: Path) -> dict[int, Proc]:
    out: dict[int, Proc] = {}
    for entry in proc_root.iterdir():
        if entry.name.isdigit():
            proc = read_proc(proc_root, int(entry.name))
            if proc:
                out[proc.pid] = proc
    for proc in out.values():
        if proc.ppid in out:
            out[proc.ppid].children.append(proc.pid)
    return out


def descendants(procs: dict[int, Proc], roots: set[int]) -> set[int]:
    seen, stack = set(), list(roots)
    while stack:
        pid = stack.pop()
        if pid in seen or pid not in procs:
            continue
        seen.add(pid)
        stack.extend(procs[pid].children)
    return seen


def user_managers(procs: dict[int, Proc], uid: int) -> set[int]:
    return {p.pid for p in procs.values()
            if p.uid == uid and p.comm == "systemd" and "--user" in p.cmdline}


def scratch_holder(lock: Path) -> int | None:
    """The pid recorded in the scratch lock (``pid N label``), or None."""
    text = (_read(lock) or "").split()
    return int(text[1]) if len(text) >= 2 and text[0] == "pid" and text[1].isdigit() else None


def boot_time(proc_root: Path) -> float | None:
    for line in (_read(proc_root / "stat") or "").splitlines():
        if line.startswith("btime "):
            return float(line.split()[1])
    return None


def live_holder(procs: dict[int, "Proc"], lock: Path, proc_root: Path, hz: int) -> int | None:
    """The lock's recorded pid, only while that pid is the process that wrote
    it: one started after the lock was last written is a reused pid
    (``_scratch.py`` never clears the pid on release)."""
    pid = scratch_holder(lock)
    if pid is None or pid not in procs:
        return None
    btime = boot_time(proc_root)
    try:
        written = lock.stat().st_mtime
    except OSError:
        return None
    if btime is None or btime + procs[pid].start_ticks / hz > written + 1:
        return None
    return pid


def in_user_service(proc: Proc, uid: int) -> bool:
    """A member of an active ``--user`` ``.service`` unit, from its cgroup path."""
    manager = f"user@{uid}.service"
    for line in proc.cgroup.splitlines():
        parts = [part for part in line.split(":", 2)[-1].split("/") if part]
        if manager in parts and any(part.endswith(".service")
                                    for part in parts[parts.index(manager) + 1:]):
            return True
    return False


def is_live_check(proc: "Proc") -> bool:
    """A ``mise run check`` process by its argv, not by a substring."""
    words = proc.argv
    return len(words) >= 3 and Path(words[0]).name == "mise" and words[1:3] == ["run", "check"]


def agent_origin(proc: Proc, origin_dirs: tuple[Path, ...]) -> bool:
    if SNAPSHOT_MARK in proc.cmdline:
        return True
    return any(proc.cwd == str(d) or proc.cwd.startswith(str(d) + "/") for d in origin_dirs)


def scan(proc_root: Path = Path("/proc"), uid: int | None = None, lock: Path = SCRATCH_LOCK,
         origin_dirs: tuple[Path, ...] = ORIGIN_DIRS, hz: int | None = None) -> list[Proc]:
    """AC1: the candidates, before the age and CPU predicates. Work of the
    scratch holder or a live check is protected by tree and by session: a
    reparented subprocess has left the tree but keeps its session."""
    uid = os.getuid() if uid is None else uid
    hz = hz or os.sysconf("SC_CLK_TCK")
    procs = processes(proc_root)
    managers = user_managers(procs, uid)
    holder = live_holder(procs, lock, proc_root, hz)
    roots = ({holder} if holder else set()) | {p.pid for p in procs.values() if is_live_check(p)}
    protected = descendants(procs, roots)
    sessions = {procs[r].session for r in roots if procs[r].session}
    return [p for p in procs.values()
            if p.uid == uid and p.ppid in managers and agent_origin(p, origin_dirs)
            and p.pid not in protected and p.session not in sessions
            and not in_user_service(p, uid)]


def classify(lifetime_pct: float, sample_pct: float) -> str | None:
    """AC2: idle, runaway, or neither."""
    if lifetime_pct < IDLE_PCT and sample_pct < IDLE_PCT:
        return "idle"
    if lifetime_pct > RUNAWAY_PCT and sample_pct > RUNAWAY_PCT:
        return "runaway"
    return None


def notify(summary: str, topic_file: str | None) -> None:
    """One ntfy POST; the topic is read from its file and never printed."""
    if not topic_file:
        print(f"orphan_reap: ntfy not sent: {NTFY_TOPIC_FILE_ENV} is not set", file=sys.stderr)
        return
    try:
        topic = Path(topic_file).read_text().strip()
        if topic:
            request = urllib.request.Request(f"https://ntfy.sh/{topic}", data=summary.encode(),
                                             headers={"Title": "orphan-reap"})
            urllib.request.urlopen(request, timeout=15).close()
    except Exception as exc:  # noqa: BLE001 - a missed alert never fails the run
        print(f"orphan_reap: ntfy not sent: {type(exc).__name__}", file=sys.stderr)


def emit_events(orphans: list[dict]) -> None:
    try:
        sys.path.insert(0, str(HERE.parent / "telemetry"))
        import emit  # noqa: PLC0415
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        events = [{"ts": now, "source": "orphan-reap", "repo": None, "issue": None,
                   "account": None, "org": None, "actor": "orphan-reap",
                   "event_type": "orphan-reap.found", "subject_kind": "process",
                   "subject_id": f"{o['pid']}:{o['start']}", "provenance": "manual",
                   "attrs": {k: o[k] for k in ("pid", "ppid", "age_s", "cpu_pct", "class", "action")}}
                  for o in orphans]
        emit.emit(events)
    except Exception as exc:  # noqa: BLE001 - telemetry never fails the run
        print(f"orphan_reap: telemetry not written: {type(exc).__name__}: {exc}", file=sys.stderr)


def run(*, proc_root: Path = Path("/proc"), age_hours: float = 6, sample_seconds: float = 60,
        lock: Path = SCRATCH_LOCK, origin_dirs: tuple[Path, ...] = ORIGIN_DIRS,
        sleep: Callable[[float], None] = time.sleep, ntfy: Callable[[str], None] | None = None,
        events: Callable[[list[dict]], None] = emit_events, uptime: float | None = None,
        hz: int | None = None, uid: int | None = None) -> list[dict]:
    """Find and report orphans. Report-only: nothing is ever signalled."""
    hz = hz or os.sysconf("SC_CLK_TCK")
    if uptime is None:
        text = _read(proc_root / "uptime")
        if not text:
            raise SystemExit("orphan_reap: cannot read /proc/uptime; no ages, so no report")
        uptime = float(text.split()[0])
    candidates = scan(proc_root, uid=uid, lock=lock, origin_dirs=origin_dirs, hz=hz)
    aged = [c for c in candidates if uptime - c.start_ticks / hz >= age_hours * 3600]
    before = {c.pid: c.cpu_ticks for c in aged}
    if sample_seconds:
        sleep(sample_seconds)  # one shared sample for every candidate (AC2)
    found = []
    for c in aged:
        after = read_proc(proc_root, c.pid)
        if after is None or after.start_ticks != c.start_ticks:
            continue
        age_s = uptime - c.start_ticks / hz
        lifetime = 100.0 * c.cpu_ticks / hz / age_s if age_s > 0 else 0.0
        sample = (100.0 * (after.cpu_ticks - before[c.pid]) / hz / sample_seconds
                  if sample_seconds else lifetime)
        cls = classify(lifetime, sample)
        if cls is None:
            continue
        found.append({"pid": c.pid, "ppid": c.ppid, "start": c.start_ticks, "age_s": int(age_s),
                      "cpu_pct": round(sample, 2), "class": cls, "action": "reported"})
    if found:
        events(found)
        summary = f"{len(found)} orphaned agent process(es): " + ", ".join(
            f"{o['pid']} {o['class']}" for o in found)
        (ntfy or (lambda s: notify(s, os.environ.get(NTFY_TOPIC_FILE_ENV))))(summary)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--age-hours", type=float, default=6)
    parser.add_argument("--sample-seconds", type=float, default=60)
    args = parser.parse_args(argv)
    found = run(age_hours=args.age_hours, sample_seconds=args.sample_seconds)
    for o in found:
        print(f"{o['pid']}\t{o['class']}\tage {o['age_s']}s\tcpu {o['cpu_pct']}%")
    if not found:
        print("orphan_reap: no orphans")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
