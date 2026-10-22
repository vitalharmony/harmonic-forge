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

A flock on the session's task directory serializes the PreToolUse sibling
check, and
`<session>/.pending/<taskId>` records an entry whose removal was allowed on
the strength of a sibling until that update reports back, so a parallel call
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
def session_lock(session: str, tasks_dir: Path | None = None):
    """An exclusive lock over `session`'s sibling check, taken on the session's
    task directory itself (pass 1: a lock file of ours could be pruned while
    held, and a lock on an unlinked inode excludes nothing; this directory is
    Claude Code's and nothing here removes it). Raises OSError when it cannot
    be taken; the caller denies."""
    fd = os.open((tasks_dir or TASKS_DIR) / session, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def pending_removals(session: str) -> set[str]:
    """Task ids whose sibling-allowed removal has not yet reported back.
    Raises OSError on an unreadable record directory; a missing one is empty.
    Pass 1 (cross-family): no TTL. A record leaves only when that task's
    TaskUpdate reaches PostToolUse or PostToolUseFailure (`clear_pending`), so
    a removal held at a prompt never expires into a second survivor; a record
    whose report never arrives keeps denying sibling removals (fail closed);
    `prune` never drops one (pass 2 sticky PATCH)."""
    directory = _dir(session) / ".pending"
    if not directory.exists():
        return set()
    return {p.name for p in directory.iterdir()}


def clear_pending(session: str, task_id: str) -> None:
    try:
        (_dir(session) / ".pending" / task_id).unlink()
    except OSError:
        pass


def mark_pending(session: str, task_id: str, key: str = "") -> None:
    """Raises OSError; the caller denies rather than allow an unrecorded removal.
    The record holds the key the removal takes the entry away from (post-verdict:
    a rename away reports back as a subject change, not a status)."""
    directory = _dir(session) / ".pending"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / task_id).write_text(key, encoding="utf-8")


def pending_key(session: str, task_id: str) -> str:
    try:
        return (_dir(session) / ".pending" / task_id).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def removal_reported(session: str, tool_input: dict) -> bool:
    """True when this TaskUpdate is the removal its pending record names: a
    completion or deletion, or a subject that no longer carries the record's key."""
    if tool_input.get("status") in ("completed", "deleted"):
        return True
    subject = tool_input.get("subject")
    if not isinstance(subject, str):
        return False
    key = pending_key(session, str(tool_input.get("taskId") or ""))
    return bool(key) and entry_key({"subject": subject}) != key


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
                    # Pass 2 (sticky PATCH): .pending records are never pruned;
                    # one left by a call that never reported only denies.
                    continue
                elif path.stat().st_mtime < cutoff:
                    path.unlink()
            directory.rmdir()
        except OSError:
            continue
