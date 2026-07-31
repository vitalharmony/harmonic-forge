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
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


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


def busy_pids(worktree: Path, exclude: set[int]) -> list[tuple[int, str]]:
    worktree = worktree.resolve()
    found: list[tuple[int, str]] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid in exclude:
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
    found = busy_pids(worktree, exclude)

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
