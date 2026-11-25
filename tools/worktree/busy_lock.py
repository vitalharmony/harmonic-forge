#!/usr/bin/env python3
"""Per-worktree busy lock held by a running `mise run check` (harmonic-forge#1560).

`mise run check` and `mise run local-check` open this module's lock path on
fd 9 and take an exclusive `flock` on it for the whole task (one marked
prelude line in each task's `run`), then record a sidecar naming the root,
the HEAD, the start time and the task shell's pid with its `/proc` start
time. `tools/hooks/worktree_busy_guard.py` reads `busy_roots()` and denies a
write into a busy worktree (R-0376).

Busy means both of these hold:

- a non-blocking shared `flock` probe on the lock fails with
  `BlockingIOError` (someone holds it exclusively), and
- the sidecar's `shell_pid` is alive and its `/proc/<pid>/stat` start time
  (field 22) equals the recorded `shell_start`.

The second clause exists because `exec 9>lock` leaves fd 9 open across exec:
a descendant that outlives the task shell keeps the kernel lock held. Once
the task shell itself is gone the worktree is not busy, whatever a leaked fd
says. Every other failure (no lock file, any other `OSError`, a missing or
unreadable sidecar) reads not busy: this module can only ever release a
write, never invent a block.

The store is `$WORKTREE_BUSY_DIR`, or `~/.claude/state/worktree-busy`
when that is unset. Tests always set `WORKTREE_BUSY_DIR`.

CLI:
  busy_lock.py path --root R              print R's lock path
  busy_lock.py sidecar --root R --head H  record the sidecar for the caller's
                                          parent shell (the task shell)
  busy_lock.py held --root R              the lock could not be taken: name the
                                          holder (a live task shell, or the
                                          processes still holding a leaked fd)
                                          and exit 1, so the check refuses
  busy_lock.py stamp --root R             record the start stamp: R's
                                          `git status --porcelain` and the time
  busy_lock.py verify --root R --head H   fail (exit 1) when HEAD moved, the
                                          porcelain changed, or a tracked file
                                          was modified since the stamp

The integrity step (design amendment issuecomment-7169440744, AC2) is what
catches a Bash write into a busy worktree: the guard hook allows Bash, and
`verify` fails the check before `check_receipt.py record` can run. A tree
dirty at the start is not a failure; `record` already refuses a dirty tree,
so that check passes with no receipt. The stamp lives in the store, outside
the worktree, and is written only after the lock is taken (or on the
fail-open path), so a refused second run never overwrites the holder's stamp.
"""
from __future__ import annotations

import argparse
import datetime
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_STATE = Path("~/.claude/state/worktree-busy")


def state_dir() -> Path:
    """`WORKTREE_BUSY_DIR`, else the default store; read at call time."""
    override = os.environ.get("WORKTREE_BUSY_DIR")
    return Path(override) if override else DEFAULT_STATE.expanduser()


def key(root: str | os.PathLike) -> str:
    return hashlib.sha256(os.path.realpath(root).encode("utf-8")).hexdigest()[:32]


def lock_path(root: str | os.PathLike) -> Path:
    return state_dir() / f"{key(root)}.lock"


def sidecar_path(root: str | os.PathLike) -> Path:
    return state_dir() / f"{key(root)}.json"


def stamp_path(root: str | os.PathLike) -> Path:
    return state_dir() / f"{key(root)}.stamp.json"


def _git(root: str | os.PathLike, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          check=True).stdout


def write_stamp(root: str | os.PathLike) -> Path:
    path = stamp_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"root": os.path.realpath(root), "stamp_ns": time.time_ns(),
              "porcelain": _git(root, "status", "--porcelain", "--untracked-files=all")}
    scratch = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    scratch.write_text(json.dumps(record), encoding="utf-8")
    os.replace(scratch, path)
    return path


def changed_since_stamp(root: str | os.PathLike, head: str) -> str | None:
    """Why the worktree changed during the check, or None. No stamp (the
    store was unavailable) reads unchanged: the lock path already failed
    open with a warning."""
    try:
        stamp = json.loads(stamp_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if _git(root, "rev-parse", "HEAD").strip() != head:
        return "HEAD moved"
    if _git(root, "status", "--porcelain", "--untracked-files=all") != stamp.get("porcelain"):
        return "the working tree changed (git status --porcelain differs from the start)"
    since = int(stamp.get("stamp_ns", 0))
    for name in _git(root, "ls-files", "-z").split("\0"):
        if not name:
            continue
        try:
            if os.lstat(os.path.join(str(root), name)).st_mtime_ns > since:
                return f"tracked file {name} was modified during the check"
        except FileNotFoundError:
            return f"tracked file {name} was removed during the check"
    return None


def fd_holders(lock: Path) -> list[int]:
    """Pids whose open fds point at `lock` (a leaked fd 9 after its task shell exited)."""
    target = os.path.realpath(lock)
    holders: list[int] = []
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            fds = list((proc / "fd").iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                if os.path.realpath(os.readlink(fd)) == target:
                    holders.append(int(proc.name))
                    break
            except OSError:
                continue
    return sorted(holders)


def held_message(root: str | os.PathLike) -> str:
    record = read_sidecar(root) or {}
    if is_busy(root):
        pid = record.get("shell_pid", "?")
        return (f"worktree-busy: a check is already running in {os.path.realpath(root)} "
                f"(shell_pid {pid}, pgid {pid}, started {record.get('started_at', '?')}); "
                f"wait for it or stop it with `kill -- -{pid}`. Refusing (R-0376).")
    holders = fd_holders(lock_path(root))
    return (f"worktree-busy: the lock for {os.path.realpath(root)} is held by a leaked fd "
            f"after its check exited; holders: {', '.join(map(str, holders)) or 'unknown'}. "
            f"Stop them, then rerun. Refusing (R-0376).")


def process_start(pid: int) -> str | None:
    """Field 22 (starttime) of `/proc/<pid>/stat`, or None if unreadable."""
    try:
        stat = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None
    # comm (field 2) is parenthesized and may hold spaces; fields resume after the last ')'.
    rest = stat.rsplit(")", 1)[-1].split()
    return rest[19] if len(rest) > 19 else None


def read_sidecar(root: str | os.PathLike) -> dict | None:
    try:
        data = json.loads(sidecar_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _lock_held(path: Path) -> bool:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _shell_alive(record: dict) -> bool:
    try:
        pid = int(record["shell_pid"])
        recorded = str(record["shell_start"])
    except (KeyError, TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    started = process_start(pid)
    return started is not None and started == recorded


def is_busy(root: str | os.PathLike) -> bool:
    """True only when the lock is held AND the recorded task shell is alive."""
    try:
        if not _lock_held(lock_path(root)):
            return False
        record = read_sidecar(root)
        return record is not None and _shell_alive(record)
    except Exception:  # noqa: BLE001 - anything unexpected reads not busy
        return False


def busy_roots() -> list[str]:
    """The roots of every sidecar in the store whose worktree `is_busy`."""
    try:
        sidecars = sorted(state_dir().glob("*.json"))
    except OSError:
        return []
    roots: list[str] = []
    for path in sidecars:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            root = data.get("root") if isinstance(data, dict) else None
        except (OSError, ValueError):
            continue
        if isinstance(root, str) and root and sidecar_path(root) == path and is_busy(root):
            roots.append(os.path.realpath(root))
    return roots


def record_sidecar(root: str | os.PathLike, head: str, shell_pid: int) -> Path:
    shell_start = process_start(shell_pid)
    if shell_start is None:
        raise OSError(f"cannot read /proc/{shell_pid}/stat")
    path = sidecar_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"root": os.path.realpath(root), "head": head,
              "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
              "shell_pid": shell_pid, "shell_start": shell_start}
    scratch = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    scratch.write_text(json.dumps(record), encoding="utf-8")
    os.replace(scratch, path)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_path = sub.add_parser("path", help="print the worktree's lock path")
    p_path.add_argument("--root", required=True)
    p_side = sub.add_parser("sidecar", help="record the sidecar for the calling task shell")
    p_side.add_argument("--root", required=True)
    p_side.add_argument("--head", required=True)
    p_held = sub.add_parser("held", help="name the lock's holder and refuse")
    p_held.add_argument("--root", required=True)
    p_stamp = sub.add_parser("stamp", help="record the start stamp")
    p_stamp.add_argument("--root", required=True)
    p_verify = sub.add_parser("verify", help="fail if the worktree changed during the check")
    p_verify.add_argument("--root", required=True)
    p_verify.add_argument("--head", required=True)
    args = parser.parse_args(argv)
    if args.command == "path":
        print(lock_path(args.root))
        return 0
    if args.command == "held":
        print(held_message(args.root), file=sys.stderr)
        return 1
    if args.command == "stamp":
        try:
            write_stamp(args.root)
        except (OSError, subprocess.CalledProcessError) as exc:
            print(f"worktree-busy: start stamp not recorded: {exc}", file=sys.stderr)
            return 1
        return 0
    if args.command == "verify":
        why = changed_since_stamp(args.root, args.head)
        if why:
            print(f"worktree-busy: {os.path.realpath(args.root)} changed during check (R-0376); "
                  f"rerun. {why}.", file=sys.stderr)
            return 1
        return 0
    try:
        record_sidecar(args.root, args.head, os.getppid())
    except OSError as exc:
        print(f"worktree-busy: sidecar not recorded: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
