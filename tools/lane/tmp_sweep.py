#!/usr/bin/env python3
"""Remove the operator's own stale test temp dirs from the real temp dir (harmonic-forge#1600).

A test run outside `tools/run_tests.py`'s containment (a session or subagent
running a test file directly) leaks every `tempfile.mkdtemp()` dir its
fixtures never clean up. On 2026-11-25 48,923 of them filled /tmp's inode cap.
Lane 1's belt tick runs this sweep, which deletes a top-level entry only when
all of these hold: its name is `tmp` plus exactly 8 of `[a-z0-9_]` (the
`mkdtemp()` default), `os.lstat` says it is a directory (a symlink is never
followed or removed), the current user owns it, and it was last modified
more than 2 hours ago. Nothing below the top level is examined.

Usage:
    python3 ~/harmonic-forge/tools/lane/tmp_sweep.py [--dry-run]

Prints `tmp-sweep: removed N` only when N > 0, so a tick that removes nothing
prints nothing; `--dry-run` lists each match and removes nothing.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path

NAME = re.compile(r"tmp[a-z0-9_]{8}")
STALE_SECONDS = 2 * 60 * 60
#: harmonic-forge#1114: a contained run records the real temp dir here.
REAL_TMPDIR_ENV = "HF_REAL_TMPDIR"


def sweep_root() -> Path:
    """The real temp dir: the recorded one in a contained run, else gettempdir()."""
    return Path(os.environ.get(REAL_TMPDIR_ENV) or tempfile.gettempdir())


def stale_matches(root: Path, now: float, uid: int) -> list[Path]:
    """Every top-level entry of `root` this sweep may remove, sorted."""
    matches: list[Path] = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return matches
    for name in names:
        if not NAME.fullmatch(name):
            continue
        path = root / name
        try:
            info = os.lstat(path)
        except OSError:
            continue  # Gone since the listing.
        if stat.S_ISDIR(info.st_mode) and info.st_uid == uid and now - info.st_mtime > STALE_SECONDS:
            matches.append(path)
    return matches


def sweep(root: Path, now: float, uid: int, dry_run: bool) -> list[Path]:
    """Remove (or with `dry_run`, only find) the stale owned matches; returns them."""
    matches = stale_matches(root, now, uid)
    if not dry_run:
        for path in matches:
            shutil.rmtree(path, ignore_errors=True)
    return matches


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Remove the current user's stale tmpXXXXXXXX dirs.")
    parser.add_argument("--dry-run", action="store_true", help="list the matches; remove nothing")
    args = parser.parse_args(argv)
    root = sweep_root()
    matches = sweep(root, time.time(), os.getuid(), args.dry_run)
    if args.dry_run:
        for path in matches:
            print(path)
        print(f"tmp-sweep --dry-run: {len(matches)} stale owned match(es) under {root}")
    elif matches:
        print(f"tmp-sweep: removed {len(matches)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
