#!/usr/bin/env python3
"""Load-aware admission for heavy jobs, across all lanes (harmonic-forge#1271).

The operator's requirement: the mechanism "needs to check actual system load to
determine what capacity is available and then use the lock mechanism
appropriately across all Lanes."

    heavy_slot.py run --kind check --label L -- <command...>
    heavy_slot.py status

## Admission

A job reserves cores and GB by kind (`heavy_slot.toml`). It is admitted when all of
these hold, re-read every 10 s:

    nproc - load1 - recent   >= its cores
    MemAvailable  - recent   >= its GB
    cpu `some avg10` and memory `some avg10` below their limits

`recent` is what jobs admitted in the last 120 s reserved, since load and memory lag
a job's start. Nothing else is subtracted: a reading already shows what an older job
uses.

Waiting is first come, first served, and bounded: past 10 minutes the head of the
queue is admitted on memory alone, unless another live job was admitted within the
last 10 minutes, so overdue jobs start one at a time. **The memory floor is never
bypassed.**

## Ownership

A process with a live holder among its `/proc` ancestors re-enters that holder, so a
`check`'s own steps never queue. Anything else passes admission, including a second
command from the same session. Holding is an `flock`, released when the holder dies.
A gate holds nothing between commands: `lane3-begin` waits for capacity once, and the
gate's wrapped commands each pass admission.

## It never stops work

In CI, or with broken state, the command runs as it would have and says so. Nothing
is refused for lacking a slot here: harmonic-forge#1272 owns enforcement.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
import tomllib
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

CONFIG = Path(__file__).with_name("heavy_slot.toml")


def _state_dir() -> Path:
    return Path(os.environ.get("HEAVY_SLOT_DIR") or Path.home() / ".cache/harmonic-forge/heavy-slot")


def _log(message: str) -> None:
    print(f"heavy-slot: {message}", file=sys.stderr, flush=True)


def load_config(path: Path = CONFIG) -> dict:
    return tomllib.loads(path.read_text())


@dataclass(frozen=True)
class Reading:
    nproc: int
    load1: float
    mem_gb: float
    cpu_avg10: float | None
    mem_avg10: float | None


def _psi(name: str) -> float | None:
    try:
        first = Path(f"/proc/pressure/{name}").read_text().splitlines()[0]
        return float(first.split("avg10=")[1].split()[0])
    except (OSError, IndexError, ValueError):
        return None


def read_system() -> Reading:
    load1 = float(Path("/proc/loadavg").read_text().split()[0])
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            available = int(line.split()[1]) / (1024 * 1024)
            return Reading(os.cpu_count() or 1, load1, available, _psi("cpu"), _psi("memory"))
    raise ValueError("/proc/meminfo has no MemAvailable")  # absent is not "0 GB free": callers fail open


def reservation(kind: str, config: dict) -> tuple[float, float]:
    """A kind's (cores, GB). A job bigger than the machine reaches the bound."""
    kinds = config["kinds"]
    entry = kinds.get(kind, kinds["default"])
    return entry["cores"], entry["gb"]


def free_capacity(reading: Reading, holders: list[dict], now: float, config: dict) -> tuple[float, float]:
    """(cores, GB) free now, less what the readings cannot show yet."""
    recent_s = config["limits"]["recent_seconds"]
    cores, gb = 0.0, 0.0
    for holder in holders:
        if now - holder["admitted_at"] < recent_s:
            cores += holder["cores"]
            gb += holder["gb"]
    return reading.nproc - reading.load1 - cores, reading.mem_gb - gb


def decide(need: tuple[float, float], reading: Reading, holders: list[dict], now: float,
           config: dict, waited: float) -> tuple[bool, str]:
    """Whether the head of the queue may start, and the numbers behind that."""
    limits = config["limits"]
    cores, gb = need
    free_cores, free_gb = free_capacity(reading, holders, now, config)
    numbers = (f"need {cores:g} cores {gb:g} GB; free {free_cores:.1f} {free_gb:.1f}; "
               f"load {reading.load1:.1f}/{reading.nproc}; cpu {reading.cpu_avg10}; mem {reading.mem_avg10}")
    if free_gb < gb:  # the floor: never bypassed, whatever has waited how long
        return False, f"memory floor: {numbers}"
    bound = limits["max_wait_seconds"]
    overlaps = any(now - h["admitted_at"] < bound for h in holders)
    if waited >= bound and not overlaps:  # one overdue start per bound, or the queue drains at once
        return True, f"after the {bound}s bound: {numbers}"
    pressure = ((reading.cpu_avg10 or 0.0) >= limits["cpu_avg10"]
                or (reading.mem_avg10 or 0.0) >= limits["mem_avg10"])
    if free_cores >= cores and free_gb >= gb and not pressure:
        return True, numbers
    return False, numbers


# --- processes -------------------------------------------------------------------


def _parent(pid: int) -> int | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("PPid:"):
                return int(line.split()[1])
    except (OSError, ValueError):
        return None
    return None


def ancestors(pid: int) -> list[int]:
    chain, seen = [], {pid}
    current = _parent(pid)
    while current and current > 1 and current not in seen:
        chain.append(current)
        seen.add(current)
        current = _parent(current)
    return chain


# --- state ------------------------------------------------------------------------


def _try_flock(path: Path) -> bool:
    """True when nobody holds `path`: its holder is gone."""
    try:
        with open(path, "a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle, fcntl.LOCK_UN)
        return True
    except BlockingIOError:
        return False
    except OSError:
        return True


def live_holders(state: Path) -> list[dict]:
    """Every live holder, removing the dead. A holder lives by its flock."""
    out = []
    directory = state / "holders"
    directory.mkdir(parents=True, exist_ok=True)
    for path in sorted(directory.glob("*.json")):
        try:
            holder = json.loads(path.read_text())
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
            continue
        if not (isinstance(holder, dict)
                and all(isinstance(holder.get(k), (int, float)) for k in ("cores", "gb", "admitted_at"))):
            path.unlink(missing_ok=True)
            path.with_suffix(".lock").unlink(missing_ok=True)
            continue
        if _try_flock(path.with_suffix(".lock")):
            path.unlink(missing_ok=True)
            path.with_suffix(".lock").unlink(missing_ok=True)
            continue
        out.append(holder)
    return out


def _tickets(state: Path) -> list[Path]:
    """Live tickets in arrival order. A dead waiter's ticket is removed, not waited on."""
    directory = state / "tickets"
    directory.mkdir(parents=True, exist_ok=True)
    live = []
    for path in sorted(directory.glob("*.ticket")):
        if _try_flock(path):
            path.unlink(missing_ok=True)
        else:
            live.append(path)
    return live


@contextlib.contextmanager
def _guard(state: Path) -> Iterator[None]:
    """One admission decision at a time, machine-wide."""
    with open(state / "admit.lock", "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _admit(kind: str, label: str, state: Path, config: dict,
           read=read_system, sleep=time.sleep, clock=time.time) -> tuple[dict, object]:
    """Wait for capacity, then record a holder. Its lock handle is returned open."""
    own_pid = os.getpid()
    ticket = state / "tickets" / f"{time.time_ns():020d}-{own_pid}.ticket"
    ticket.parent.mkdir(parents=True, exist_ok=True)
    with _guard(state):
        ticket_handle = open(ticket, "a+")
        fcntl.flock(ticket_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    started, last_note = clock(), 0.0
    try:
        while True:
            reading = read()
            need = reservation(kind, config)
            with _guard(state):
                now = clock()
                holders = live_holders(state)
                head = next(iter(_tickets(state)), ticket) == ticket
                ok, numbers = decide(need, reading, holders, now, config, now - started)
                if head and ok:
                    holder_id = uuid.uuid4().hex
                    holder = {"id": holder_id, "pid": own_pid, "kind": kind, "label": label,
                              "lane": os.environ.get("LANE", ""), "cores": need[0], "gb": need[1],
                              "admitted_at": now}
                    directory = state / "holders"
                    handle = open(directory / f"{holder_id}.lock", "a+")
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    scratch = directory / f"{holder_id}.tmp"
                    scratch.write_text(json.dumps(holder))
                    os.replace(scratch, directory / f"{holder_id}.json")
                    _log(f"admitted {kind} {label}: {numbers}")
                    return holder, handle
            if now - last_note >= config["limits"]["note_seconds"]:
                who = ", ".join(f"{h['lane'] or '?'} {h['kind']} {h['label']}" for h in holders) or "load"
                _log(f"waiting on {who} for {int(now - started)}s ({numbers})")
                last_note = now
            sleep(config["limits"]["poll_seconds"])
    finally:
        ticket_handle.close()
        ticket.unlink(missing_ok=True)


def _ancestor_holder(state: Path) -> dict | None:
    chain = set(ancestors(os.getpid())) | {os.getpid()}
    return next((h for h in live_holders(state) if h["pid"] in chain), None)


def _unmanaged_reason() -> str | None:
    """Why this run takes no slot. A test suite must not sit in the machine's real queue."""
    if os.environ.get("CI"):
        return "CI"
    return "HEAVY_SLOT_DISABLE" if os.environ.get("HEAVY_SLOT_DISABLE") else None


@contextlib.contextmanager
def slot(kind: str, label: str) -> Iterator[None]:
    """Hold a slot for this block. It fails open: broken state warns and runs."""
    reason, holder, handle, state = _unmanaged_reason(), None, None, _state_dir()
    if reason:
        _log(f"{reason}: running {kind} {label} unmanaged")
    else:
        try:
            config = load_config()
            state.mkdir(parents=True, exist_ok=True)
            with _guard(state):
                parent = _ancestor_holder(state)
            if parent:
                _log(f"{kind} {label} runs under {parent['kind']} {parent['label']}")
            else:
                holder, handle = _admit(kind, label, state, config)
        except Exception as exc:  # noqa: BLE001 -- AC6: admission can never stop the command
            _log(f"WARNING: running {kind} {label} unmanaged: {type(exc).__name__}: {exc}")
    try:
        yield
    finally:
        if holder:
            with contextlib.suppress(OSError):
                (state / "holders" / f"{holder['id']}.json").unlink(missing_ok=True)
                (state / "holders" / f"{holder['id']}.lock").unlink(missing_ok=True)
        if handle:
            handle.close()


def cmd_run(args) -> int:
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        print("heavy_slot.py run: no command", file=sys.stderr)
        return 2
    with slot(args.kind, args.label):
        child = subprocess.Popen(command)
        for number in (signal.SIGTERM, signal.SIGINT):
            signal.signal(number, lambda sig, _frame: child.send_signal(sig))
        return child.wait()


def cmd_status(_args) -> int:
    config, state, now = load_config(), _state_dir(), time.time()
    state.mkdir(parents=True, exist_ok=True)
    reading = read_system()
    with _guard(state):
        holders = live_holders(state)
        queued = len(_tickets(state))
    cores, gb = free_capacity(reading, holders, now, config)
    print(f"load {reading.load1:.1f}/{reading.nproc}; MemAvailable {reading.mem_gb:.1f} GB; "
          f"cpu avg10 {reading.cpu_avg10}; mem avg10 {reading.mem_avg10}; free {cores:.1f} cores {gb:.1f} GB")
    for holder in holders:
        print(f"  run {holder['lane'] or '?'} {holder['kind']} {holder['label']} "
              f"for {int(now - holder['admitted_at'])}s ({holder['cores']:g} cores {holder['gb']:g} GB)")
    print(f"  queued: {queued}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load-aware admission for heavy jobs.")
    sub = parser.add_subparsers(dest="action", required=True)
    run = sub.add_parser("run")
    run.add_argument("--kind", required=True)
    run.add_argument("--label", default="")
    run.add_argument("command", nargs=argparse.REMAINDER)
    run.set_defaults(func=cmd_run)
    sub.add_parser("status").set_defaults(func=cmd_status)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
