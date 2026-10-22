#!/usr/bin/env python3
"""The owed task-list update store (harmonic-forge#1126 AC2).

A lane post, a merge, a PR or a close changes what an issue's task-list entry
must say. This module records that the change is owed; `tasklist_hold.py`'s
Stop hook refuses to end the turn until the entry has been rewritten. The
entry shape itself lives in `tasklist_hold.py`'s docstring, not here.

Modeled on `handoff_owed.py`: per-session files keyed on
`CLAUDE_CODE_SESSION_ID` (a Stop hook can never be blocked by another
session's record), prune by mtime after `PRUNE_AFTER_DAYS`, and every failure
is silent -- `record()` never raises into a post that already succeeded.

Layout: `~/.cache/harmonic-forge/tasklist_owed/<session>/<KEY>.json`, one file
per issue key, holding `{key, kind, ts}`. Reforge epoch 4: a marker is cleared
only by `tasklist_hold.py`'s PostToolUse hook, when a successful TaskCreate or
TaskUpdate gives an entry carrying the key its subject; nothing compares task
files against the marker.

`<session>/.lock` serializes the PreToolUse sibling check, and
`<session>/.pending/<taskId>` records an entry whose removal was just allowed
on the strength of a sibling, for `PENDING_TTL` seconds, so a parallel call
never counts it as the survivor (epoch 3 pass 2, survivor 1).
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path

OWED_DIR = Path.home() / ".cache" / "harmonic-forge" / "tasklist_owed"
TASKS_DIR = Path.home() / ".claude" / "tasks"
PRUNE_AFTER_DAYS = 7
#: Seconds a sibling-allowed removal stays pending (well over a hook's 10 s).
PENDING_TTL = 60
OPEN_STATUSES = ("pending", "in_progress")
KEY_RE = re.compile(r"^([HFIOPKYD])(\d+)\b")


def session_id() -> str:
    return os.environ.get("CLAUDE_CODE_SESSION_ID", "") or ""


def key_for(repo: str, issue: int | str) -> str | None:
    """`vitalharmony/hrse`, 12 -> `H12`, via the one prefix table
    (`batch_auth.REPO_PREFIXES`); None when it cannot be resolved."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import batch_auth  # noqa: PLC0415
        return batch_auth.issue_key(repo, issue)
    except Exception:
        return None


def read_entries(session: str, tasks_dir: Path | None = None) -> list[dict]:
    """Every task entry of `session`, each with `_mtime`. Raises OSError when
    the directory or any `.json` entry cannot be read: a skipped entry would be
    judged absent, so the caller cannot judge and allows (reforge epoch 1)."""
    base = (tasks_dir or TASKS_DIR) / session
    entries = []
    for path in base.iterdir():
        if path.suffix != ".json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            mtime = path.stat().st_mtime
        except (OSError, ValueError) as exc:
            raise OSError(f"{path}: {exc}") from exc
        if isinstance(data, dict):
            data["_mtime"] = mtime
            entries.append(data)
    return entries


def entry_key(entry: dict) -> str | None:
    match = KEY_RE.match(str(entry.get("subject") or ""))
    return f"{match.group(1)}{match.group(2)}" if match else None


def open_entries(entries: list[dict], key: str) -> list[dict]:
    return [e for e in entries if e.get("status") in OPEN_STATUSES and entry_key(e) == key]


def _dir(session: str) -> Path:
    return OWED_DIR / session


def record(key: str, kind: str, session: str | None = None,
           now: float | None = None) -> bool:
    """Record that `key`'s entry owes an update after `kind`. Never raises."""
    session = session_id() if session is None else session
    if not session:
        # No Stop hook can ever match an unkeyed marker; writing one would
        # only litter the store (and is what a test run, which unsets the
        # session id in run_tests.py, would otherwise do).
        return False
    try:
        path = _dir(session) / f"{key}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        # One marker per key, always overwritten; the latest wins.
        # The write is atomic: a killed hook leaves the old marker or the new
        # one, never a truncated file read as no marker.
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({
            "key": key, "kind": kind,
            "ts": now if now is not None else time.time(),
        }), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        return False
    prune(now)
    return True


def record_post(repo: str, issue: int | str, kind: str) -> None:
    """The post scripts' call: resolve the key and record. Never raises, so a
    post that succeeded never reports a failure from here."""
    try:
        key = key_for(repo, issue)
        if key:
            record(key, kind)
    except Exception:
        pass


def markers(session: str) -> list[dict]:
    try:
        files = sorted(_dir(session).glob("*.json"))
    except OSError:
        return []
    found = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("key"):
            found.append(data)
    return found


@contextmanager
def session_lock(session: str):
    """An exclusive lock over `session`'s sibling check. Raises OSError when
    it cannot be taken; the caller denies."""
    directory = _dir(session)
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / ".lock", "a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def pending_removals(session: str, now: float | None = None) -> set[str]:
    """Task ids whose removal was allowed within `PENDING_TTL`. Raises OSError
    on an unreadable record directory; a missing one is empty."""
    directory = _dir(session) / ".pending"
    if not directory.exists():
        return set()
    cutoff = (now if now is not None else time.time()) - PENDING_TTL
    return {p.name for p in directory.iterdir() if p.stat().st_mtime >= cutoff}


def mark_pending(session: str, task_id: str) -> None:
    """Raises OSError; the caller denies rather than allow an unrecorded removal."""
    directory = _dir(session) / ".pending"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / task_id).write_text("", encoding="utf-8")


def clear(session: str, key: str) -> None:
    try:
        (_dir(session) / f"{key}.json").unlink()
    except OSError:
        pass


def prune(now: float | None = None) -> None:
    """Drop markers untouched for `PRUNE_AFTER_DAYS`, by each marker file's own
    mtime (an overwrite in place never touches its directory's), then any
    session directory left empty. Never raises."""
    cutoff = (now if now is not None else time.time()) - PRUNE_AFTER_DAYS * 86400
    try:
        dirs = [d for d in OWED_DIR.iterdir() if d.is_dir()]
    except OSError:
        return
    for directory in dirs:
        try:
            for path in directory.iterdir():
                if path.is_dir():
                    for record in path.iterdir():
                        if record.stat().st_mtime < cutoff:
                            record.unlink()
                    path.rmdir()
                elif path.stat().st_mtime < cutoff:
                    path.unlink()
            directory.rmdir()
        except OSError:
            continue
