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

#: Where procfs is read. One name, so a test can point it at a fixture (a
#: regular file, an empty directory) and every read below follows it.
PROC = Path("/proc")


def read_lane(pid: int) -> str | None:
    """Return the LANE env var of `pid`, or None if unreadable/unset."""
    try:
        raw = PROC.joinpath(str(pid), "environ").read_bytes()
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
            stat = PROC.joinpath(str(current), "stat").read_text()
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


class CannotTell(Exception):
    """procfs is not readable here, so no answer is an answer (harmonic-forge#1531)."""


def probe_procfs() -> None:
    """Raise CannotTell when there is no procfs at PROC (harmonic-forge#1531).

    PROC/self, not PROC: an empty /proc directory (procfs not mounted) would
    list no process and read as idle. os.stat, not Path.exists: whether
    exists() swallows EACCES depends on the Python version. Only "not there"
    (absent, or a /proc that is not a directory) means cannot tell; a denied
    stat (PermissionError) propagates, and main reads it as busy (exit 1, fail
    closed). Callers run it before any other procfs read, the ancestor scan
    included, so a /proc that is a regular file never escapes as an error."""
    try:
        os.stat(PROC / "self")
    except (FileNotFoundError, NotADirectoryError):
        raise CannotTell(f"no procfs at {PROC}") from None


def busy_pids(worktree: Path, exclude: set[int], caller_lane: str | None) -> list[tuple[int, str]]:
    # harmonic-forge#1531: probed here too, not only in main, so worktree_gc
    # gets it (its sweep skips a candidate whose check raises).
    probe_procfs()
    worktree = worktree.resolve()
    found: list[tuple[int, str]] = []
    for entry in PROC.iterdir():
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

    caller_lane = os.environ.get("LANE") or None
    # harmonic-forge#1531: a host with no procfs (macOS, or /proc not mounted)
    # cannot tell. Exit 3, not the busy 1: only the launcher refresh treats 3 as
    # not busy (with a warning); gate-checkout and lane1_rebase keep refusing on
    # it, and worktree_gc skips the candidate. The probe runs before the
    # ancestor scan, which reads procfs too: with /proc a regular file that
    # scan would raise NotADirectoryError out of main instead of exiting 3.
    try:
        probe_procfs()
        exclude = ancestor_pids(os.getpid())
        found = busy_pids(worktree, exclude, caller_lane)
    except CannotTell as exc:
        print(f"check-worktree-busy: cannot tell ({exc})", file=sys.stderr)
        return 3
    except PermissionError as exc:
        print(f"check-worktree-busy: refusing — procfs is not readable ({exc}); "
              "treating the worktree as busy", file=sys.stderr)
        return 1

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
