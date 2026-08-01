#!/usr/bin/env python3
"""Detect live processes with cwd inside a shared per-lane git worktree.

Standing check from harmonic-forge#137: a per-lane worktree (HRSE2-lane2/3,
harmonic-forge-lane2/3, and equivalents in other repos) is reused
sequentially across different issues, with no lock preventing two actors
from targeting it at once. A `git checkout`/`rebase` there while a dev
server or live test is still running yanks branch/process state out from
under whatever's in progress, with no error to signal it happened. This
script is the guard: exit 1 (and print who's running) if anything other
than the caller's own process tree has its cwd inside the target worktree.

harmonic-forge#162: ancestor-only exclusion false-positives on two classes
of legitimate same-session process that are never an ancestor of the
calling Bash command: (1) session-wide MCP tooling (e.g. workspace-mcp)
spawned once at Claude Code startup, and (2) deliberately-detached dev
services launched via `systemd-run --user` (hrse#459) specifically to
survive a Bash call's cgroup being collected. Both are legitimately
excludable because `LANE` (harmonic-forge#142) is inherited by (1)
through normal process ancestry from the wrapping `lane1/2/3` script, and
can be explicitly propagated into (2) via `--setenv`. A process whose own
environment carries the same LANE value as the caller is treated as part
of the same session and excluded, regardless of ancestry.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def read_lane(pid: int) -> str | None:
    """Return the LANE env var of `pid`, or None if unreadable/unset."""
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None
    for entry in raw.split(b"\0"):
        if entry.startswith(b"LANE="):
            return entry[len(b"LANE="):].decode(errors="replace")
    return None


def ancestor_pids(pid: int) -> set[int]:
    """Return pid plus every ancestor up to pid 1, so a caller can exclude itself."""
    ancestors = {pid}
    current = pid
    while current > 1:
        try:
            stat = Path(f"/proc/{current}/stat").read_text()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            break
        # comm is parenthesized and may itself contain ')'; split on the
        # LAST ')' to safely reach ppid (field 4) regardless of comm content.
        after_comm = stat.rsplit(")", 1)[-1].split()
        try:
            ppid = int(after_comm[1])
        except (IndexError, ValueError):
            break
        ancestors.add(ppid)
        current = ppid
    return ancestors


def busy_pids(worktree: Path, exclude: set[int], caller_lane: str | None) -> list[tuple[int, str]]:
    worktree = worktree.resolve()
    found: list[tuple[int, str]] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid in exclude:
            continue
        if caller_lane and read_lane(pid) == caller_lane:
            continue
        try:
            cwd = Path(os.readlink(entry / "cwd"))
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if cwd != worktree and worktree not in cwd.parents:
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except (FileNotFoundError, PermissionError):
            raw = b""
        cmdline = raw.replace(b"\0", b" ").decode(errors="replace").strip() or "<unknown>"
        found.append((pid, cmdline))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("worktree", nargs="?", default=".", help="Worktree path to check (default: cwd)")
    args = parser.parse_args()

    worktree = Path(args.worktree)
    if not worktree.is_dir():
        print(f"check-worktree-busy: '{worktree}' is not a directory", file=sys.stderr)
        return 2

    exclude = ancestor_pids(os.getpid())
    caller_lane = os.environ.get("LANE") or None
    found = busy_pids(worktree, exclude, caller_lane)

    if found:
        print(
            f"check-worktree-busy: refusing — live process(es) with cwd inside {worktree.resolve()}:",
            file=sys.stderr,
        )
        for pid, cmdline in found:
            print(f"  pid {pid}: {cmdline}", file=sys.stderr)
        print(
            "check-worktree-busy: wait for these to exit, ask the operator, or do prep work "
            "in a disposable scratch worktree instead",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
