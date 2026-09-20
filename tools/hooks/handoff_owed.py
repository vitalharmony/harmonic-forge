#!/usr/bin/env python3
"""The owed-handoff store: R-0039's second half, made into state (harmonic-forge#687).

R-0039 says filing an issue and posting its handoff are **one atomic
action** -- *"'File it' means 'file it with its artifact,' in one action"*.
forge#516 merged that text and shipped no enforcement, so a session could
satisfy the first half and stop while every mechanical signal it received
said it had succeeded: `gh_issue.py` printed `Created issue: <url>` and
exited 0, and nothing anywhere recorded that a handoff was now owed.

This module is the missing state. `gh_issue.py` calls `record()` the
moment an issue exists; `enforce_handoff_owed.py` (a Stop hook) reads
`outstanding()` and refuses to end the turn; a posted handoff, or a
recorded R-0039 exception, calls `discharge()`.

## Keyed on the session, not on one shared file

Per-session files under `~/.cache/harmonic-forge/handoff_owed/`, following
`session_model.py` and `enforce_belt_arming.py`'s existing convention for
hook state. The key matters more than the format: a Stop hook that cannot
tell this session's obligation from a dead session's would block every
future turn in every lane, with no action available that clears it. That
is the worst failure this module can have, and per-session files make it
structurally impossible rather than merely unlikely.

Both halves can see the same key today: Claude Code sets
`CLAUDE_CODE_SESSION_ID` in the environment (which `gh_issue.py` inherits
as a Bash subprocess) and passes the same identifier to Stop hooks as
`payload["session_id"]`.

## `discharge()` is deliberately NOT session-scoped

`record()` writes to one session's file; `discharge()` sweeps every
session's. A handoff can legitimately be posted by a different session
than the one that filed -- Lane 1 routinely posts for an issue a no-LANE
session filed. Scoping the clear to the caller's own session would leave
the filer's obligation standing forever with the handoff already live on
the issue, which is the "escape hatch is a lie" failure
`block_missing_preclose_inspection.py` was corrected out of.

Keyed on repo AND issue number, never on issue number alone: `hrse#687`
and `harmonic-forge#687` are different issues.

## Stale records

A session that files an issue and then dies leaves its file behind. The
per-session key already bounds the blast radius -- that file can never
block anyone else -- but it would otherwise accumulate forever, so
`record()` prunes files whose mtime is older than `PRUNE_AFTER_DAYS`
(currently 7). Seven days is long past any live session and short enough
that the directory stays readable by a human debugging it.

## Every failure here is silent and non-blocking

A hook that can wedge a session into never ending its turn is worse than
one that misses a stop -- `block_batch_stop.py`'s own contract, and it
applies identically. Every function in this module swallows OSError and
returns the safe value: `record()` gives up rather than raising into the
middle of a successful issue creation, and `outstanding()` returns `[]`
rather than blocking on an unreadable file.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

#: Hook-state location, matching `session_model.py` and `enforce_belt_arming.py`.
OWED_DIR = Path.home() / ".cache" / "harmonic-forge" / "handoff_owed"

#: See the module docstring. Files older than this are removed on the next
#: `record()`; nothing else sweeps them, on purpose -- a scheduled cleaner
#: would be more moving parts than an mtime comparison deserves.
PRUNE_AFTER_DAYS = 7

#: R-0039's three filing-bar exceptions, verbatim in slug form. An exception
#: outside this set is refused rather than accepted as free text: the point of
#: recording one is that it names a rule, not that it names something.
EXCEPTIONS = ("parent-epic", "deferred-design-record", "blocked-on-prerequisite")


def session_id() -> str:
    """This process's session key, or `""`.

    `gh_issue.py` runs as a Bash subprocess of the session and inherits the
    variable. An empty value is not an error: the record is still written,
    under a key no Stop hook will ever match, so it becomes an audit trail
    rather than a block. Blocking an arbitrary future session because this
    one could not identify itself is the failure this avoids.
    """
    return os.environ.get("CLAUDE_CODE_SESSION_ID", "") or ""


def _path(key: str) -> Path:
    return OWED_DIR / f"{key or 'no-session'}.json"


def _read(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def _write(path: Path, entries: list[dict]) -> bool:
    try:
        OWED_DIR.mkdir(parents=True, exist_ok=True)
        if entries:
            path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        elif path.exists():
            path.unlink()
        return True
    except OSError:
        return False


def prune(now: float | None = None) -> None:
    """Drop session files older than `PRUNE_AFTER_DAYS`. Never raises."""
    cutoff = (now if now is not None else time.time()) - PRUNE_AFTER_DAYS * 86400
    try:
        entries = list(OWED_DIR.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            continue


def record(repo: str, issue: int, url: str = "", key: str | None = None,
           now: float | None = None) -> bool:
    """Record that `repo#issue` owes a handoff. Idempotent per (repo, issue).

    Called the moment an issue exists and **before** anything that can fail
    -- see `gh_issue.py`'s call site. That ordering is the whole of AC4: the
    observed failure (hrse#1919) created the issue and then exited non-zero
    on a board-field write, and a record written afterwards would have been
    the one thing the incident proves does not happen.
    """
    key = session_id() if key is None else key
    path = _path(key)
    entries = [e for e in _read(path)
               if not (e.get("repo") == repo and e.get("issue") == issue)]
    entries.append({
        "repo": repo,
        "issue": issue,
        "url": url,
        "recorded_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(now if now is not None else time.time())),
    })
    ok = _write(path, entries)
    prune(now)
    return ok


def outstanding(key: str) -> list[dict]:
    """Every unposted handoff owed by session `key`, oldest first."""
    return _read(_path(key))


def discharge(repo: str, issue: int) -> int:
    """Clear `repo#issue` from EVERY session's file. Returns the count cleared.

    Cross-session on purpose -- see the module docstring. Returning a count
    rather than a bool lets a caller stay quiet when there was nothing to
    clear, which is the normal case for a handoff on an issue filed by hand.
    """
    try:
        files = [f for f in OWED_DIR.iterdir() if f.is_file()]
    except OSError:
        return 0
    cleared = 0
    for path in files:
        entries = _read(path)
        kept = [e for e in entries
                if not (e.get("repo") == repo and e.get("issue") == issue)]
        if len(kept) != len(entries):
            cleared += len(entries) - len(kept)
            _write(path, kept)
    return cleared


def discharge_command(repo: str, issue: int) -> str:
    """The literal command that discharges this obligation."""
    return (f"mise run l1-post -- --repo {repo} --issue {issue} "
            f"--kind handoff --file <handoff.md> --sha <sha> --branch main")
