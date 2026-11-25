#!/usr/bin/env python3
"""The integrity step of a busy-locked check (harmonic-forge#1560, AC2).

`busy_lock.py begin` writes a start stamp after it takes the lock (or on the
fail-open path, where no lock could be taken); `busy_lock.py end` reads it back
immediately before `check_receipt.py record`. The stamp lives in the busy-lock
store, outside the worktree, and carries:

- a per-run `nonce`, which `begin` hands the task shell and the shell passes to
  `end`. A stamp with another nonce (a stale stamp from an earlier run, or one
  a second run overwrote on the fail-open path) is a failure, never a pass;
- the worktree's `git status --porcelain --untracked-files=all`;
- one observation per tracked path: `[st_ino, st_ctime_ns, st_mode]` from
  `lstat`, or the errno name of the `OSError` that `lstat` raised. A tree
  already odd at the start (a path deleted, ENOTDIR under a file-occupied
  parent, ELOOP, EACCES, skip-worktree or sparse paths) is observed the same
  way at both ends, so only a difference fails. Equality against the stamp's
  own observation, never an "after the stamp time" comparison, so no clock or
  ctime ordering is involved.

ctime, not mtime: a tool can set or preserve mtime (`touch -d`, `cp -p`,
`rsync -a`, `tar x`), but every write, rename or chmod of an inode sets its
ctime to now and no userland call sets it back.

What this detects, and what it does not (sticky-wicket verdict on F1560,
reforged epoch, AC2 scope): it detects a persistent content change, a HEAD or
porcelain change, and an in-place or replace rewrite of any tracked file. A
tracked file or directory renamed aside and restored, or any change undone
without rewriting a tracked file's inode, is not detected. Directory ctimes are
deliberately not compared: Python writes `__pycache__` beside the sources on a
first import, which would fail the first check in every fresh worktree.
"""
from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
from pathlib import Path


def git(root: str | os.PathLike, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          errors="surrogateescape", check=True).stdout


def observe(root: str | os.PathLike) -> dict[str, object]:
    """Each tracked path's `[st_ino, st_ctime_ns, st_mode]`, or its errno name."""
    seen: dict[str, object] = {}
    for name in git(root, "ls-files", "-z").split("\0"):
        if not name:
            continue
        try:
            st = os.lstat(os.path.join(str(root), name))
            seen[name] = [st.st_ino, st.st_ctime_ns, st.st_mode]
        except OSError as exc:
            seen[name] = errno.errorcode.get(exc.errno or 0, str(exc.errno))
    return seen


def write_stamp(path: Path, root: str | os.PathLike, nonce: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"root": os.path.realpath(root), "nonce": nonce,
              "porcelain": git(root, "status", "--porcelain", "--untracked-files=all"),
              "observed": observe(root)}
    scratch = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    scratch.write_text(json.dumps(record), encoding="utf-8")
    os.replace(scratch, path)


def changed(path: Path, root: str | os.PathLike, head: str, nonce: str) -> str | None:
    """Why the worktree changed during the check, or None when it did not.

    Every error is a reason: the caller holds a nonce, so a stamp was written,
    and anything that stops this from comparing it fails the check."""
    try:
        stamp = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"the start stamp {path} is missing or unreadable ({exc})"
    if not isinstance(stamp, dict) or stamp.get("nonce") != nonce:
        return f"the start stamp {path} is not this run's (another run wrote it)"
    if stamp.get("root") != os.path.realpath(root):
        return f"the start stamp {path} names another worktree"
    if git(root, "rev-parse", "HEAD").strip() != head:
        return "HEAD moved"
    if git(root, "status", "--porcelain", "--untracked-files=all") != stamp.get("porcelain"):
        return "the working tree changed (git status --porcelain differs from the start)"
    before = stamp.get("observed")
    now = json.loads(json.dumps(observe(root)))
    if not isinstance(before, dict):
        return f"the start stamp {path} has no observations"
    for name in sorted(set(before) | set(now)):
        if before.get(name) != now.get(name):
            return f"tracked path {name} changed during the check ({before.get(name)} -> {now.get(name)})"
    return None


def end(path: Path, root: str, head: str, nonce: str) -> int:
    """`busy_lock.py end`: 0 verified, 3 unverifiable (no nonce), 1 changed."""
    if not nonce:
        print("worktree-busy: no receipt: integrity stamp unavailable, this check is "
              "unverified", file=sys.stderr)
        return 3
    try:
        why = changed(path, root, head, nonce)
    except Exception as exc:  # noqa: BLE001 - a nonce means a stamp was written
        why = f"the integrity step could not run ({type(exc).__name__}: {exc})"
    if why:
        print(f"worktree-busy: {os.path.realpath(root)} changed during check (R-0376); "
              f"rerun. {why}.", file=sys.stderr)
        return 1
    return 0
