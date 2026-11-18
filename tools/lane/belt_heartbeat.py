#!/usr/bin/env python3
"""The belt's heartbeat (harmonic-forge#1398): one file the watcher writes and
every suspenders tick reads.

The watcher (`tools/gh/watch_lane_posts.py`) writes it once at the end of
every poll. A tick (the `/loop 30m` cron) reads its age: a fresh heartbeat
means the Monitor is alive, a stale one means it died or expired and the tick
may re-arm it (belt skill step 4), but only when no Monitor task for the belt
is live either, so a tick landing in the normal expiry-to-re-arm gap never
double-arms.

The watcher writes it, and nothing else does: a session that only calls this
module's `read` cannot fake a fresh heartbeat without the watcher process
running. It is a liveness file and nothing more. It is never a lane action:
`record_lane_action` in `tools/hooks/afk_toggle.py`'s state directory owns that,
and a heartbeat that reset the afk idle clock would defeat the cutoff.

One file per (lane, workspace, scope). `scope` is the watcher's `--repo` value
or `all` (harmonic-forge#1199 lets two sessions of one lane split a workspace).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path

STATE_RELDIR = Path(".claude") / "state" / "belt"
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def state_dir() -> Path:
    return Path.home() / STATE_RELDIR


def _slug(value: str) -> str:
    return _UNSAFE.sub("_", value).strip("_") or "x"


def path_for(lane: str, workspace: str, scope: str = "all") -> Path:
    return state_dir() / f"heartbeat-{_slug(lane)}-{_slug(workspace)}-{_slug(scope)}.json"


def write(lane: str, workspace: str, scope: str = "all", deadline: float | None = None,
          now: float | None = None) -> Path:
    """Record one poll. Atomic: a unique temp file renamed into place, so a
    reader never sees a half-written file."""
    path = path_for(lane, workspace, scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"written_at": time.time() if now is None else now,
                       "deadline": deadline, "pid": os.getpid()})
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def read(lane: str, workspace: str, scope: str = "all", now: float | None = None) -> float | None:
    """The heartbeat's age in seconds, or None when there is no readable one.

    None is never "fresh": a missing, unparseable or future-dated file reads
    as no heartbeat, which is the direction that lets a tick re-arm."""
    try:
        written = float(json.loads(path_for(lane, workspace, scope).read_text(encoding="utf-8"))["written_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    age = (time.time() if now is None else now) - written
    return age if age >= 0 else None


def is_stale(lane: str, workspace: str, scope: str = "all", max_age: float = 600.0,
             now: float | None = None) -> bool:
    """True when there is no heartbeat or it is older than `max_age` (two poll
    intervals of 300 s by default)."""
    age = read(lane, workspace, scope, now)
    return age is None or age > max_age
