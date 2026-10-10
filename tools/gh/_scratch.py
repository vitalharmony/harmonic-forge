"""One owner for scratch-check disk space (harmonic-forge#949).

Every scratch check (the `l1_post` pre-post check, `kill_check`, HRSE2's
`ci_parity_check`) copies a repo and installs its dependencies, more than 1 GB
each. Under `tempfile.mkdtemp()` that landed on the 16 GB `/tmp` tmpfs, and
three concurrent runs filled it three times in one session (ENOSPC in every
session on the machine). A run killed by SIGKILL also leaked its directory,
because cleanup was a `finally:` that never runs.

`scratch_dir()` is the only way to get one:
- **Root on a real disk:** `~/.cache/hrse-scratch`, overridable by
  `HRSE_SCRATCH_ROOT`. Never `/tmp`.
- **One at a time, machine-wide:** an exclusive `flock` on `<root>/.lock`,
  held for the whole run. A second run waits up to `HRSE_SCRATCH_LOCK_WAIT_S`
  (default 3600 s), then refuses, naming the holder.
- **Leak-proof:** each directory carries an `.owner` file (pid, start time,
  label). While holding the lock, every directory whose owner pid is dead is
  deleted before a new one is made, so a killed run is reclaimed by the next
  run with no sweep job.
- **Space preflight:** refuses when the root's filesystem has less than
  `HRSE_SCRATCH_MIN_FREE_GB` (default 10) free, reporting free space and the
  largest directories under the root.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

OWNER = ".owner"
# The prefixes this module creates. Creation and reaping both run under the
# lock, so an ownerless directory with one of these is a half-created one of
# ours whose process died before writing its owner file.
PREFIXES = ("scratch-", "l1-post-", "kill-check-")


class ScratchError(RuntimeError):
    """A scratch directory could not be provided; the message says why."""


def root() -> Path:
    """Resolved per call, so a test (or a caller) that moves HOME is honored.

    Refuses a root that resolves under /tmp or the system temp dir (the tmpfs
    this module exists to keep scratch checks off)."""
    base = Path(os.environ.get("HRSE_SCRATCH_ROOT") or Path.home() / ".cache" / "hrse-scratch").resolve()
    for tmp in {Path("/tmp").resolve(), Path(tempfile.gettempdir()).resolve()}:
        if base == tmp or tmp in base.parents:
            raise ScratchError(f"scratch root {base} is under {tmp}; set HRSE_SCRATCH_ROOT to a disk-backed path")
    return base


def _proc_start(pid: int) -> str | None:
    """Field 22 of /proc/<pid>/stat (start time in clock ticks), or None."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return stat.rsplit(")", 1)[1].split()[19]


def _alive(owner: dict) -> bool:
    """The owner process still runs: its pid exists and, where recorded, its
    start time matches, so a reused pid does not keep a dead run's dir."""
    try:
        pid = int(owner.get("pid", 0))
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    recorded = owner.get("proc_start")
    return recorded is None or _proc_start(pid) == recorded


def _owner(path: Path) -> dict | None:
    try:
        return json.loads((path / OWNER).read_text())
    except (OSError, ValueError):
        return None


def reap(base: Path) -> list[Path]:
    """Delete every scratch directory under `base` whose owner is dead.

    A directory with no readable owner file is ours only if it carries one of
    `PREFIXES` (a run killed between creating it and writing the owner file);
    any other ownerless directory belongs to a tool that pointed `TMPDIR` here
    and is left alone."""
    removed = []
    for child in sorted(base.iterdir()) if base.is_dir() else []:
        if not child.is_dir() or child.name.startswith("."):
            continue
        owner = _owner(child)
        if owner is None:
            if not child.name.startswith(PREFIXES):
                continue
        elif _alive(owner):
            continue
        shutil.rmtree(child, ignore_errors=True)
        removed.append(child)
    return removed


def _largest(base: Path, n: int = 3) -> str:
    sizes = []
    for child in base.iterdir() if base.is_dir() else []:
        total = 0
        for dirpath, _, files in os.walk(child):
            for name in files:
                with contextlib.suppress(OSError):
                    total += os.lstat(os.path.join(dirpath, name)).st_size
        sizes.append((total, child.name))
    sizes.sort(reverse=True)
    return ", ".join(f"{name} {size / 2**30:.1f} GB" for size, name in sizes[:n]) or "none"


def preflight(base: Path) -> None:
    floor = float(os.environ.get("HRSE_SCRATCH_MIN_FREE_GB", "10"))
    free = shutil.disk_usage(base).free / 2**30
    if free < floor:
        raise ScratchError(
            f"scratch root {base} has {free:.1f} GB free, below the {floor:g} GB floor "
            f"(HRSE_SCRATCH_MIN_FREE_GB); largest under it: {_largest(base)}")


@contextlib.contextmanager
def _lock(base: Path, label: str) -> Iterator[None]:
    wait = float(os.environ.get("HRSE_SCRATCH_LOCK_WAIT_S", "3600"))
    path = base / ".lock"
    with open(path, "a+") as handle:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    handle.seek(0)
                    holder = handle.read().strip() or "unknown"
                    raise ScratchError(
                        f"another scratch check holds {path} ({holder}); waited {wait:g} s")
                time.sleep(2)
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid {os.getpid()} {label}")
        handle.flush()
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextlib.contextmanager
def scratch_dir(label: str, prefix: str = "scratch-") -> Iterator[Path]:
    """A fresh scratch directory under the disk-backed root, one run at a time.

    The directory is removed on exit; if the process is killed, the next
    caller reaps it."""
    base = root()
    base.mkdir(parents=True, exist_ok=True)
    with _lock(base, label):
        reap(base)
        preflight(base)
        path = Path(tempfile.mkdtemp(prefix=prefix, dir=base))
        try:
            (path / OWNER).write_text(json.dumps({"pid": os.getpid(), "proc_start": _proc_start(os.getpid()),
                                                  "started": time.time(), "label": label}))
            yield path
        finally:
            shutil.rmtree(path, ignore_errors=True)
