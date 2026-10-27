#!/usr/bin/env python3
"""Orphaned agent processes: find, report, and stop only an allowlist
(harmonic-forge#1181 AC1-AC4). Deterministic and LLM-free.

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
that found any sends one ntfy summary. Only two classes are ever stopped: a
``podman-compose ... up`` and a ``python3 -c`` process, and only once an
earlier run reported the same (pid, start time) at least ``--grace-hours``
ago. Signals go through pidfd after a start-time check, re-checked before
SIGKILL. Everything else is report-only. ``--dry-run`` is the default; the
timer unit passes ``--execute``.

Process facts come from ``/proc`` only. ``proc_root`` and the signal and
clock functions are injectable, so the tests run on fixture trees and never
touch a real process.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
STATE_PATH = Path.home() / ".cache" / "harmonic-forge" / "orphan_reap_state.json"
SCRATCH_LOCK = Path.home() / ".cache" / "hrse-scratch" / ".lock"
ORIGIN_DIRS = (Path.home() / ".cache" / "hrse-scratch", Path.home() / "Harmonic_Projects" / ".worktrees")
SNAPSHOT_MARK = "/.claude/shell-snapshots/"
IDLE_PCT, RUNAWAY_PCT = 1.0, 50.0
KILL_WAIT_S = 30
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
        ppid, utime, stime, start = int(rest[1]), int(rest[11]), int(rest[12]), int(rest[19])
        uid = int(next(line.split()[1] for line in status.splitlines() if line.startswith("Uid:")))
    except (ValueError, IndexError, StopIteration):
        return None
    cmdline = (_read(base / "cmdline") or "").replace("\0", " ").strip()
    try:
        cwd = os.readlink(base / "cwd")
    except OSError:
        cwd = ""
    return Proc(pid=pid, ppid=ppid, uid=uid, comm=(_read(base / "comm") or "").strip(),
                cmdline=cmdline, cwd=cwd, start_ticks=start, cpu_ticks=utime + stime,
                cgroup=(_read(base / "cgroup") or "").strip())


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


def in_user_service(proc: Proc, uid: int) -> bool:
    """A member of an active ``--user`` ``.service`` unit, from its cgroup path."""
    for line in proc.cgroup.splitlines():
        path = line.split(":", 2)[-1]
        parts = [part for part in path.split("/") if part]
        if f"user@{uid}.service" in parts and parts and parts[-1].endswith(".service") \
                and parts[-1] != f"user@{uid}.service":
            return True
    return False


def agent_origin(proc: Proc, origin_dirs: tuple[Path, ...]) -> bool:
    if SNAPSHOT_MARK in proc.cmdline:
        return True
    return any(proc.cwd == str(d) or proc.cwd.startswith(str(d) + "/") for d in origin_dirs)


def scan(proc_root: Path = Path("/proc"), uid: int | None = None, lock: Path = SCRATCH_LOCK,
         origin_dirs: tuple[Path, ...] = ORIGIN_DIRS) -> list[Proc]:
    """AC1: the candidates, before the age and CPU predicates."""
    uid = os.getuid() if uid is None else uid
    procs = processes(proc_root)
    managers = user_managers(procs, uid)
    holder = scratch_holder(lock)
    protected = descendants(procs, {holder} if holder else set())
    checks = {p.pid for p in procs.values() if "mise run check" in p.cmdline}
    protected |= descendants(procs, checks)
    return [p for p in procs.values()
            if p.uid == uid and p.ppid in managers and agent_origin(p, origin_dirs)
            and p.pid not in protected and not in_user_service(p, uid)]


def classify(lifetime_pct: float, sample_pct: float) -> str | None:
    """AC2: idle, runaway, or neither."""
    if lifetime_pct < IDLE_PCT and sample_pct < IDLE_PCT:
        return "idle"
    if lifetime_pct > RUNAWAY_PCT and sample_pct > RUNAWAY_PCT:
        return "runaway"
    return None


def allowlisted(cmdline: str) -> bool:
    """AC4: exactly two stoppable classes."""
    words = cmdline.split()
    if not words:
        return False
    name = Path(words[0]).name
    if name == "podman-compose" or (name.startswith("python") and len(words) > 1
                                     and Path(words[1]).name == "podman-compose"):
        return "up" in words
    return name.startswith("python3") and len(words) > 1 and words[1] == "-c"


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and all(isinstance(v, (int, float)) for v in data.values()) else {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".orphan_reap_state.")
    with os.fdopen(fd, "w") as handle:
        json.dump(state, handle, sort_keys=True)
    os.replace(tmp, path)


def _pidfd_signal(pid: int, sig: int) -> None:
    fd = os.pidfd_open(pid)
    try:
        signal.pidfd_send_signal(fd, sig)
    finally:
        os.close(fd)


def stop(proc_root: Path, pid: int, start: int, *, send: Callable[[int, int], None] = _pidfd_signal,
         sleep: Callable[[float], None] = time.sleep, wait_s: float = KILL_WAIT_S) -> str:
    """SIGTERM, then SIGKILL after ``wait_s``, each only while (pid, start) still match."""
    current = read_proc(proc_root, pid)
    if current is None or current.start_ticks != start:
        return "gone-or-reused"
    send(pid, signal.SIGTERM)
    sleep(wait_s)
    current = read_proc(proc_root, pid)
    if current is None or current.start_ticks != start:
        return "terminated"
    send(pid, signal.SIGKILL)
    return "killed"


def notify(summary: str, topic_file: str | None) -> None:
    """One ntfy POST; the topic is read from its file and never printed."""
    if not topic_file:
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


def run(*, proc_root: Path = Path("/proc"), execute: bool = False, age_hours: float = 6,
        grace_hours: float = 1, sample_seconds: float = 60, state_path: Path = STATE_PATH,
        lock: Path = SCRATCH_LOCK, origin_dirs: tuple[Path, ...] = ORIGIN_DIRS,
        now: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
        send: Callable[[int, int], None] = _pidfd_signal, ntfy: Callable[[str], None] | None = None,
        events: Callable[[list[dict]], None] = emit_events, uptime: float | None = None,
        hz: int | None = None) -> list[dict]:
    hz = hz or os.sysconf("SC_CLK_TCK")
    if uptime is None:
        uptime = float((_read(proc_root / "uptime") or "0").split()[0])
    candidates = scan(proc_root, lock=lock, origin_dirs=origin_dirs)
    aged = [c for c in candidates if uptime - c.start_ticks / hz >= age_hours * 3600]
    before = {c.pid: c.cpu_ticks for c in aged}
    if sample_seconds:
        sleep(sample_seconds)  # one shared sample for every candidate (AC2)
    state = load_state(state_path)
    seen_before = dict(state)
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
        key = f"{c.pid}:{c.start_ticks}"
        first = seen_before.get(key)
        action = "reported"
        if allowlisted(c.cmdline) and first is not None and now() - first >= grace_hours * 3600:
            action = stop(proc_root, c.pid, c.start_ticks, send=send, sleep=sleep) if execute \
                else "would-stop"
        state.setdefault(key, now())
        found.append({"pid": c.pid, "ppid": c.ppid, "start": c.start_ticks, "age_s": int(age_s),
                      "cpu_pct": round(sample, 2), "class": cls, "action": action})
    live = {f"{c.pid}:{c.start_ticks}" for c in candidates}
    save_state(state_path, {k: v for k, v in state.items() if k in live})
    if found:
        events(found)
        summary = f"{len(found)} orphaned agent process(es): " + ", ".join(
            f"{o['pid']} {o['class']} {o['action']}" for o in found)
        (ntfy or (lambda s: notify(s, os.environ.get(NTFY_TOPIC_FILE_ENV))))(summary)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True,
                      help="report only (the default for a manual run)")
    mode.add_argument("--execute", action="store_true", help="stop allowlisted orphans past grace")
    parser.add_argument("--age-hours", type=float, default=6)
    parser.add_argument("--grace-hours", type=float, default=1)
    parser.add_argument("--sample-seconds", type=float, default=60)
    args = parser.parse_args(argv)
    found = run(execute=args.execute, age_hours=args.age_hours, grace_hours=args.grace_hours,
                sample_seconds=args.sample_seconds)
    for o in found:
        print(f"{o['pid']}\t{o['class']}\t{o['action']}\tage {o['age_s']}s\tcpu {o['cpu_pct']}%")
    if not found:
        print("orphan_reap: no orphans")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
