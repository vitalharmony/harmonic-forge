#!/usr/bin/env python3
"""Debris reaping for tmux_lanes_claude.sh (hrse#2017).

A killed lane session (crashed terminal, `kill -9`, a lost tmux server) never
runs its own shutdown path, so it leaves two classes of debris behind that
`--stop-lanes` cannot see, by construction:

- A `~/.cache/harmonic-forge/*.lock` belt/sweep lock
  (`watch_lane_posts.py`'s `_acquire_belt_lock`): held via `flock` for the
  holder's process lifetime and released automatically by the kernel the
  instant that process dies, however it dies -- but the JSON payload
  describing the (now dead) holder is left on disk with nothing to remove it.
- A worktree dev server stack (`pc-up`, `mise.toml [tasks.pc-up]`) started
  outside the tmux pane tree entirely -- it carries no `--session-id`, so
  `lane_sessions`' own selector in the shell script cannot see it either.

Kept out of the shell script (which embeds `lane_sessions` as an
unsourceable inline heredoc) specifically so these two functions are
importable and unit-testable -- see `test_lanes_reap.py`.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

#: Zero-byte flock targets in the same directory, not session locks --
#: excluded by name rather than relied on to fail the JSON-shape check
#: below (hrse#2017's own Notes section asks for this explicitly).
NON_SESSION_LOCK_NAMES = {"gh_core_budget.json.lock", "gh_scan_override.lock"}

#: Lane 2/3 worktree checkouts are named "<repo>-lane2"/"<repo>-lane3" by
#: convention (see HRSE2's lane-protocol.md and harmonic-forge's
#: check_worktree_busy.py docstring: "HRSE2-lane2/3, harmonic-forge-lane2/3,
#: and equivalents in other repos"); the main checkout's own command line
#: never contains either segment, which is what keeps AC4 true.
_WORKTREE_PATTERN = re.compile(r"-lane[23]/")
_DEV_SERVER_PATTERN = re.compile(r"\b(uvicorn|vite)\b")
_WORKTREE_ROOT_TOKEN = re.compile(r"^(.*-lane[23])/")

#: Any process whose cwd is inside a lane worktree AND whose cmdline matches
#: this is part of our own dev-server stack, not a foreign occupant -- the
#: process-compose supervisor itself never matches _DEV_SERVER_PATTERN (its
#: own command line is just "process-compose -f ..."), so it needs its own
#: allowance here or every reap would see it and wrongly call the worktree
#: busy (hrse#2017 preclose finding).
_STACK_PROCESS_PATTERN = re.compile(r"\b(uvicorn|vite|process-compose)\b")


def _cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode(errors="replace")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, TypeError):
        return False
    except PermissionError:
        return True
    return True


def reap_stale_belt_locks(lock_dir: Path) -> list[tuple[str, dict]]:
    """Remove every `lock_dir/*.lock` whose holder is dead.

    Liveness is tested the same way the lock is acquired in the first
    place -- a non-blocking `flock` attempt -- rather than by re-parsing
    the recorded `pid`/`session_pid` fields, because that IS the
    authoritative test: if the flock succeeds, no process holds it, full
    stop, regardless of what the stale JSON payload claims. If it fails,
    a live process holds it and this function must never touch it (AC2).

    The flock is held continuously from the moment it is acquired through
    `unlink()` -- never released first -- so no other process's `open()` +
    `flock()` on this same path can succeed in between and be orphaned by
    our delete (hrse#2017 preclose finding: releasing before unlinking left
    exactly that window open).

    Returns `[(filename, payload), ...]` for every lock actually removed.
    `lock_dir` not existing is not an error -- nothing to reap.
    """
    if not lock_dir.is_dir():
        return []

    removed: list[tuple[str, dict]] = []
    for path in sorted(lock_dir.glob("*.lock")):
        if path.name in NON_SESSION_LOCK_NAMES:
            continue
        try:
            fh = open(path, "r+")
        except OSError:
            continue
        with fh:
            try:
                raw = fh.read()
            except OSError:
                continue
            try:
                payload = json.loads(raw) if raw.strip() else None
            except (json.JSONDecodeError, ValueError):
                payload = None
            if (
                not isinstance(payload, dict)
                or "pid" not in payload
                or "session_pid" not in payload
            ):
                # Not a recognized session-lock payload -- leave it alone
                # rather than guess.
                continue
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue  # a live process holds it -- never remove this
            # Holding the flock here proves the prior owner is dead. Unlink
            # now, while still holding it and still inside the `with fh:`
            # block -- closing (which releases the flock) happens only
            # after the path is already gone.
            try:
                path.unlink()
            except OSError:
                continue
        removed.append((path.name, payload))
    return removed


def _worktree_root_from_cmdline(cmdline: str) -> str | None:
    for token in cmdline.split():
        m = _WORKTREE_ROOT_TOKEN.match(token)
        if m:
            return m.group(1)
    return None


def _process_compose_port(worktree_root: Path) -> str | None:
    """`PROCESS_COMPOSE_PORT` for `worktree_root`, read from its own
    `.mise.local.toml` (see mise.toml [tasks.pc-up]/[tasks.pc-down]: the
    stack's systemd unit is named "hrse-process-compose-<port>.service").
    A plain regex, not a TOML parser, is enough for this one known key in a
    file this project writes itself."""
    try:
        text = (worktree_root / ".mise.local.toml").read_text()
    except OSError:
        return None
    m = re.search(r'PROCESS_COMPOSE_PORT\s*=\s*"(\d+)"', text)
    return m.group(1) if m else None


def _foreign_occupants(worktree_root: Path, exclude_pids: set[int]) -> list[tuple[int, str]]:
    """Every live process (other than `exclude_pids`) whose cwd is inside
    `worktree_root` and whose cmdline is NOT part of the dev-server stack
    itself (`_STACK_PROCESS_PATTERN`) -- the same cwd-based ownership test
    `check_worktree_busy.py` uses (harmonic-forge#137), applied here to
    decide whether a worktree's dev-server stack is safe to stop rather
    than whether a checkout is safe to switch.

    A live Codex (or Claude, or a shell, an editor, a running test --
    anything) session working in the worktree shows up here and blocks the
    stop; `lane_sessions guard` in the shell script only ever confirmed
    Claude sessions are dead, which said nothing about a non-Claude
    occupant (hrse#2017 preclose finding)."""
    found: list[tuple[int, str]] = []
    try:
        worktree_root = worktree_root.resolve()
    except OSError:
        return found
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid in exclude_pids:
            continue
        try:
            cwd = Path(os.readlink(f"/proc/{pid}/cwd"))
        except OSError:
            continue
        if cwd != worktree_root and worktree_root not in cwd.parents:
            continue
        cmd = _cmdline(pid)
        if _STACK_PROCESS_PATTERN.search(cmd):
            continue  # our own stack, not a foreign occupant
        found.append((pid, cmd or "<unknown>"))
    return found


def _default_stop_unit(unit: str) -> bool:
    """`systemctl --user stop <unit>` -- the exact mechanism mise.toml's own
    `pc-down` task uses, chosen specifically because SIGTERMing the uvicorn
    child directly does not free the port: process-compose.yaml declares
    `hrse-backend` with `availability.restart: "exit_on_failure"`, so the
    supervisor just restarts it (hrse#2017 preclose finding). Stopping the
    systemd unit takes the supervisor down too, which is what actually
    releases the port. Returns False (no-op) if the unit isn't active --
    nothing to stop, not a failure."""
    active = subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", unit]
    ).returncode == 0
    if not active:
        return False
    subprocess.run(["systemctl", "--user", "stop", unit])
    return True


def _terminate_pids(pids: list[int]) -> list[int]:
    """SIGTERM, then SIGKILL after a 5s grace period, for every pid still
    alive. Returns the pids still alive afterward (should be empty).
    Fallback path only -- used when a matched worktree's stack has no
    resolvable/active systemd unit (not started by `pc-up`)."""
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.time() + 5
    while time.time() < deadline and any(_alive(p) for p in pids):
        time.sleep(0.25)
    for pid in pids:
        if _alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    kill_deadline = time.time() + 2
    while time.time() < kill_deadline and any(_alive(p) for p in pids):
        time.sleep(0.05)
    return [p for p in pids if _alive(p)]


def reap_orphaned_lane_worktree_servers(*, stop_unit=_default_stop_unit) -> list[dict]:
    """For every Lane 2/3 worktree with a live `uvicorn`/`vite` process
    still bound to it: if nothing else (Claude, Codex, a shell, a test) is
    also live inside that worktree, stop its dev-server stack via its own
    systemd unit (falling back to directly signalling the matched pids only
    if no such unit is active) and report what happened; otherwise skip it
    and report why.

    Callers must only invoke this after confirming every previous lane
    Claude session is already dead (`lane_sessions guard` in the shell
    script) -- that establishes no *Claude* session still owns the
    worktree, which is necessary but not sufficient on its own (Codex is
    also a documented Lane 2/3 actor -- see the `_foreign_occupants` check
    this function performs itself before touching anything).

    `stop_unit` is injectable for tests; production uses
    `_default_stop_unit` (real `systemctl --user`).

    Returns one dict per worktree with debris found:
    `{"worktree": str, "action": "stopped-unit"|"stopped-pids"|"skipped-busy",
      ...}`.
    """
    candidates: dict[str, list[tuple[int, str]]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        cmd = _cmdline(pid)
        if not (cmd and _WORKTREE_PATTERN.search(cmd) and _DEV_SERVER_PATTERN.search(cmd)):
            continue
        root = _worktree_root_from_cmdline(cmd)
        if root:
            candidates.setdefault(root, []).append((pid, cmd))

    results: list[dict] = []
    for root, procs in candidates.items():
        worktree_root = Path(root)
        known_pids = {pid for pid, _ in procs}
        foreign = _foreign_occupants(worktree_root, known_pids)
        if foreign:
            results.append({
                "worktree": root,
                "action": "skipped-busy",
                "foreign": foreign,
            })
            continue

        port = _process_compose_port(worktree_root)
        unit = f"hrse-process-compose-{port}.service" if port else None
        if unit and stop_unit(unit):
            results.append({
                "worktree": root,
                "action": "stopped-unit",
                "unit": unit,
                "pids": sorted(known_pids),
            })
            continue

        survivors = _terminate_pids(sorted(known_pids))
        results.append({
            "worktree": root,
            "action": "stopped-pids",
            "pids": sorted(known_pids),
            "survivors": survivors,
        })
    return results


def _main(argv: list[str]) -> int:
    if not argv:
        print("usage: lanes_reap.py reap-belt-locks DIR | reap-worktree-servers", file=sys.stderr)
        return 2
    if argv[0] == "reap-belt-locks":
        if len(argv) != 2:
            print("usage: lanes_reap.py reap-belt-locks DIR", file=sys.stderr)
            return 2
        for name, payload in reap_stale_belt_locks(Path(argv[1])):
            print(
                f"lanes: removed stale lock {name} "
                f"(pid {payload.get('pid')}, session {payload.get('session_pid')})"
            )
        return 0
    if argv[0] == "reap-worktree-servers":
        for result in reap_orphaned_lane_worktree_servers():
            worktree = result["worktree"]
            if result["action"] == "stopped-unit":
                print(f"lanes: stopped orphaned worktree dev-server stack {result['unit']} "
                      f"(worktree {worktree}, pids {result['pids']})")
            elif result["action"] == "stopped-pids":
                print(f"lanes: stopped orphaned worktree dev server pids {result['pids']} "
                      f"(worktree {worktree}, no active systemd unit found)")
                if result["survivors"]:
                    print(f"lanes: WARNING -- pid(s) {result['survivors']} in {worktree} "
                          "did not die", file=sys.stderr)
            elif result["action"] == "skipped-busy":
                names = ", ".join(f"{pid} ({cmd[:60]})" for pid, cmd in result["foreign"])
                print(f"lanes: NOT reaping {worktree} -- other live process(es) present: {names}")
        return 0
    print(f"unknown subcommand: {argv[0]}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
