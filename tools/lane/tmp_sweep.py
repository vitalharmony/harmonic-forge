#!/usr/bin/env python3
"""Remove the user's stale `mkdtemp()` dirs from the real temp dir (harmonic-forge#1600).

Only a top-level entry named `tmp` + 8 of `[a-z0-9_]`, a dir per `os.lstat`, user-owned
and over 2 hours old. Stdlib only, never probing temp space: it must run with /tmp full.
Prints `tmp-sweep: removed N` only when N > 0; `--dry-run` lists and removes nothing."""
from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import sys
import time
from pathlib import Path

NAME = re.compile(r"tmp[a-z0-9_]{8}")
STALE_SECONDS = 2 * 60 * 60
#: harmonic-forge#1114: a contained run records the real temp dir here.
REAL_TMPDIR_ENV = "HF_REAL_TMPDIR"


def sweep_root() -> Path:
    """HF_REAL_TMPDIR, else TMPDIR, else /tmp: never gettempdir(), whose probe moves off a full dir."""
    return Path(os.environ.get(REAL_TMPDIR_ENV) or os.environ.get("TMPDIR") or "/tmp")


def remove_tree(directory: Path) -> str | None:
    """Private copy of tools/gh/kill_check.py's remove_tree: repair permissions, then remove."""
    if not directory.exists():
        return None
    try:
        directory.chmod(stat.S_IRWXU)
        for root, dirs, _files in os.walk(directory, topdown=True, followlinks=False):
            for name in dirs:
                child = Path(root) / name
                if not child.is_symlink():
                    child.chmod(stat.S_IRWXU)
        shutil.rmtree(directory)
    except OSError as exc:
        return str(exc)
    return None if not directory.exists() else f"{directory} still exists"


def stale_matches(root: Path, now: float, uid: int) -> list[Path]:
    """Every top-level entry of `root` this sweep may remove, sorted."""
    matches: list[Path] = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return matches
    for name in filter(NAME.fullmatch, names):
        path = root / name
        try:
            info = os.lstat(path)
        except OSError:
            continue  # Gone since the listing.
        if stat.S_ISDIR(info.st_mode) and info.st_uid == uid and now - info.st_mtime > STALE_SECONDS:
            matches.append(path)
    return matches


def sweep(root: Path, now: float, uid: int, dry_run: bool) -> list[Path]:
    """With `dry_run`, the stale owned matches; else the ones actually removed."""
    matches = stale_matches(root, now, uid)
    if dry_run:
        return matches
    for path in matches:
        remove_tree(path)
    return [path for path in matches if not os.path.lexists(path)]


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
