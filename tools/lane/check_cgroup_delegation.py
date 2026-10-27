#!/usr/bin/env python3
"""Fail unless the systemd user manager delegates the cpu and memory cgroup
controllers (harmonic-forge#1181 AC6).

Without them a ``systemd-run --user --scope -p MemoryHigh=... -p CPUQuota=...``
runs with no limit and no error: the scope simply has no ``memory.high`` or
``cpu.max`` file. The fix is one root step, see DROP_IN below; re-log (or
``systemctl daemon-reexec`` and restart the user manager) afterwards.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REQUIRED = ("cpu", "memory")
DROP_IN = "/etc/systemd/system/user@.service.d/delegate.conf"
DROP_IN_TEXT = "[Service]\nDelegate=cpu memory pids\n"


def controllers_path(uid: int, root: Path = Path("/sys/fs/cgroup")) -> Path:
    return root / "user.slice" / f"user-{uid}.slice" / f"user@{uid}.service" / "cgroup.controllers"


def missing(uid: int | None = None, root: Path = Path("/sys/fs/cgroup")) -> list[str]:
    """The required controllers the user manager does not have; all of them
    when the file cannot be read (fail closed)."""
    path = controllers_path(os.getuid() if uid is None else uid, root)
    try:
        present = set(path.read_text().split())
    except OSError:
        return list(REQUIRED)
    return [name for name in REQUIRED if name not in present]


def main() -> int:
    absent = missing()
    if absent:
        print(f"cgroup delegation: {', '.join(absent)} not delegated to the user manager. "
              f"As root, write {DROP_IN} containing:\n{DROP_IN_TEXT}"
              "then re-log (harmonic-forge#1181 AC6).", file=sys.stderr)
        return 1
    print("cgroup delegation: cpu and memory delegated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
