#!/usr/bin/env python3
"""Per-worktree busy lock held by a running `mise run check` (harmonic-forge#1560).

`mise run check` and `mise run local-check` open this module's lock path on
fd 9 in the task shell, then run `begin --fd 9` (one marked prelude line in
each task's `run`). `begin` takes an exclusive `flock` on the inherited fd
(a 5 s non-blocking retry); the lock belongs to the open file description, so
it stays held after `begin` exits, until the task shell and every descendant
close fd 9. `begin` then records a sidecar naming the root, the HEAD, the start
time and the task shell's pid (`--pid $$`) with its `/proc` start time, writes
the integrity stamp (`busy_stamp.py`) and prints `nonce=<value>`, quoted, for
the shell to `eval`. `tools/hooks/worktree_busy_guard.py` reads `busy_roots()`
and denies a file-tool write into a busy worktree (R-0376).

Busy means both of these hold:

- a non-blocking shared `flock` probe on the lock fails with
  `BlockingIOError` (someone holds it exclusively), and
- the sidecar's `shell_pid` is alive and its `/proc/<pid>/stat` start time
  (field 22) equals the recorded `shell_start`.

The second clause exists because fd 9 stays open across exec: a descendant
that outlives the task shell keeps the kernel lock held. Once the task shell
itself is gone the worktree is not busy, whatever a leaked fd says. Every
other failure (no lock file, any other `OSError`, a missing or unreadable
sidecar) reads not busy: this module can only ever release a write, never
invent a block.

A run has three outcomes (sticky-wicket verdict on F1560, as revised after
pitch inspection; AC6/AC7). Unverifiable: `begin` could not write the stamp,
so it prints an empty nonce and warns; `end` exits 3 and the task exits 0
without running `check_receipt.py record` (unavailable lock infrastructure
never fails a check, and never yields a receipt). Changed: a nonce is present
but the stamp is missing, unreadable, another run's, or differs, or anything
stops the compare; `end` exits 1 and the task fails. Verified: `end` exits 0
and `record` runs. Only a held lock makes `begin` exit non-zero. An unusable
lock fd is the fail-open path: no sidecar, a warning, and still a stamp.

The store is `$WORKTREE_BUSY_DIR`, else `~/.claude/state/worktree-busy`.

CLI: `path --root R` (the lock path); `begin --root R --fd N --pid P`;
`end --root R --head H --nonce N`; `sidecar --root R --head H` (records the
caller's parent shell; the tests' holders use it).
"""
from __future__ import annotations

import argparse
import datetime
import fcntl
import hashlib
import json
import os
import secrets
import shlex
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


def fd_holders(lock: Path, shell_pid: int | None = None) -> list[int]:
    """Pids whose open fds point at `lock` (a leaked fd 9 after its task shell
    exited). The caller, its parent and the refusing task shell (`shell_pid`),
    which opened fd 9 themselves, are not holders and are left out."""
    target = os.path.realpath(lock)
    own = {os.getpid(), os.getppid(), shell_pid}
    holders: list[int] = []
    for proc in Path("/proc").glob("[0-9]*"):
        if int(proc.name) in own:
            continue
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


def held_message(root: str | os.PathLike, shell_pid: int | None = None) -> str:
    record = read_sidecar(root) or {}
    if is_busy(root):
        pid = record.get("shell_pid", "?")
        return (f"worktree-busy: a check is already running in {os.path.realpath(root)} "
                f"(shell_pid {pid}, pgid {pid}, started {record.get('started_at', '?')}); "
                f"wait for it or stop it with `kill -- -{pid}`. Refusing (R-0376).")
    holders = fd_holders(lock_path(root), shell_pid)
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


def _take(fd: int) -> bool | None:
    """Lock the inherited `fd`: True taken, False held by another after 5 s,
    None when it cannot be locked at all (the fail-open path)."""
    deadline = time.monotonic() + 5
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)
        except OSError:
            return None


def begin(root: str, fd: str, pid: int) -> int:
    """Lock, sidecar, stamp; print `nonce=`. Exits non-zero only when held."""
    def warn(message: str) -> None:
        print(f"worktree-busy: {message}", file=sys.stderr)
    try:
        taken = _take(int(fd)) if fd else None
    except Exception:  # noqa: BLE001 - an unusable fd is the fail-open path
        taken = None
    if taken is False:
        try:
            message = held_message(root, pid)
        except Exception as exc:  # noqa: BLE001 - still refuse
            message = f"worktree-busy: the lock for {root} is held ({exc}). Refusing (R-0376)."
        print(message, file=sys.stderr)
        return 1
    if not taken:
        warn("lock not taken")
    else:
        try:
            record_sidecar(root, subprocess.run(["git", "-C", root, "rev-parse", "HEAD"],
                                                capture_output=True, text=True).stdout.strip(), pid)
        except Exception as exc:  # noqa: BLE001
            warn(f"sidecar not recorded: {exc}")
    nonce = ""
    try:
        import busy_stamp
        nonce = secrets.token_hex(8)
        busy_stamp.write_stamp(stamp_path(root), root, nonce)
    except Exception as exc:  # noqa: BLE001 - unverifiable: no receipt, never a failure
        nonce = ""
        warn(f"no receipt: integrity stamp unavailable ({type(exc).__name__}: {exc})")
    print(f"nonce={shlex.quote(nonce)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("path", "sidecar", "begin", "end"):
        sub.add_parser(name).add_argument("--root", required=True)
    sub.choices["sidecar"].add_argument("--head", required=True)
    sub.choices["begin"].add_argument("--fd", default="")
    sub.choices["begin"].add_argument("--pid", type=int, required=True)
    sub.choices["end"].add_argument("--head", required=True)
    sub.choices["end"].add_argument("--nonce", default="")
    args = parser.parse_args(argv)
    if args.command == "path":
        print(lock_path(args.root))
        return 0
    if args.command == "begin":
        return begin(args.root, args.fd, args.pid)
    if args.command == "end":
        import busy_stamp  # an import failure exits 1: changed, never unverifiable
        return busy_stamp.end(stamp_path(args.root), args.root, args.head, args.nonce)
    try:
        record_sidecar(args.root, args.head, os.getppid())
    except OSError as exc:
        print(f"worktree-busy: sidecar not recorded: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
