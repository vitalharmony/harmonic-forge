#!/usr/bin/env python3
"""The untracked-file check for the test step (harmonic-forge#1518).

A test run must leave the checkout as it found it. One test cleared `os.environ`
and reached the real `gh`, which then wrote `.local/state/gh/device-id` into the
working directory, and `mise run commit`'s `git add .` swept the file into a commit.
`run_tests.py` records the untracked files before the suite and compares after it:
any path that is new fails the run and is named. It lives here, not in `run_tests.py`,
because that file is already over the 300-line cap (R-0006).

The comparison is "new since before", never "empty after": a checkout may already
hold untracked files, and those are not this run's. Ignored files are not counted
(`--exclude-standard`). When git is missing or the root is not a work tree the check
cannot run, and it says so on stderr with the exit code that stands; it never
reports a pass for a check that did not run.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def snapshot(root: Path) -> set[str] | None:
    """Untracked, non-ignored paths under the git work tree at `root`, or None when
    git is unavailable or `root` is not a work tree. NUL-delimited, so a path with a
    space or a newline is one path."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--others", "--exclude-standard"],
            capture_output=True, check=False)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return {path.decode("utf-8", "surrogateescape") for path in result.stdout.split(b"\0") if path}


def new_paths(before: set[str], after: set[str]) -> list[str]:
    """Paths in `after` that `before` did not hold, sorted."""
    return sorted(after - before)


def verdict(before: set[str] | None, root: Path, code: int) -> int:
    """The run's final exit code, given the suite's own `code` (0 or 1).

    A new untracked path fails the run (1) and is named, whatever the suite's result.
    When the check could not run, before or after, one stderr line says so and the
    suite's own code stands."""
    after = snapshot(root) if before is not None else None
    if before is None or after is None:
        print(f"[test] the untracked-file check did not run: git is unavailable or {root} "
              f"is not a git work tree. The suite's own exit code stands: {code}.", file=sys.stderr)
        return code
    new = new_paths(before, after)
    if not new:
        return code
    print(f"[test] FAIL: the run left {len(new)} untracked file(s) in {root} "
          "(harmonic-forge#1518); a test must not write into the checkout:", file=sys.stderr)
    for path in new:
        print(f"[test]   {path}", file=sys.stderr)
    return 1
