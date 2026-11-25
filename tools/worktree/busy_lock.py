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
"""
from __future__ import annotations

import argparse
import datetime
import fcntl
import hashlib
import json
import os
import sys
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
    args = parser.parse_args(argv)
    if args.command == "path":
        print(lock_path(args.root))
        return 0
    try:
        record_sidecar(args.root, args.head, os.getppid())
    except OSError as exc:
        print(f"worktree-busy: sidecar not recorded: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
