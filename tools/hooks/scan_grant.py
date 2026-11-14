#!/usr/bin/env python3
"""The SCAN grant: the operator lets one session run one bounded gh scan of one
repo and scope (harmonic-forge#1363, `rules/lane-shorthand.md` § SCAN).

`SCAN <letter> <scope>: <reason>`, typed as the prompt's first line, unindented
and uppercase, is honored by `expand_lane_shorthand.py` on `UserPromptSubmit`
(R-0117, the provenance BATCH, FORCE, PARK and UI use). Unlike PARK it needs no
LANE: a wayfinder run is a no-lane session. It writes
`<session>/<LETTER>-<scope digest>.json` under `SCAN_STATE_DIR` (default
`~/.claude/state/scan/`).

`tools/gh/bounded_scan.py` requires `live(repo, scope, session)`; the gh shim
admits a `bounded_scan` scan only with `live_for_repo(repo, session)`. A grant
is reusable within its TTL and is never consumed: the snapshot cache, not the
grant, keeps reruns cheap. `prune` drops files untouched for `PRUNE_AFTER_DAYS`.

Like BATCH, FORCE, IMC and PARK, this trusts the operator-typed line and the
session: the grant file is an audit trail, not proof against a session that
writes this directory itself (the accepted residual in the handoff).
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "gh"))
import imc_waiver  # noqa: E402  (time and atomic-write helpers, one copy)

#: `None` means `~/.claude/state/scan`, resolved at call time, so a test that
#: patches this global or `HOME` never reaches the live store.
SCAN_STATE_DIR: Path | None = None
STATE_RELPATH = Path(".claude") / "state" / "scan"
REAL_SCAN_DIR = Path.home() / STATE_RELPATH
TESTING_ENV = "HARMONIC_FORGE_TESTING"
TTL_HOURS = 2.0
PRUNE_AFTER_DAYS = 7
HOOK_VERSION = "1"

#: The vitalharmony repo prefixes (`rules/lane-shorthand.md` § Repo prefixes).
REPOS = {"H": "vitalharmony/hrse", "F": "vitalharmony/harmonic-forge",
         "I": "vitalharmony/cymagraph-infra", "O": "vitalharmony/openclaw-projects"}
#: Other accounts, separate credentials: a vitalharmony session's scan of one
#: returns empty, never an error (the same table's warning), so it is refused.
CROSS_ACCOUNT = frozenset({"K", "Y", "D", "P"})

_SESSION_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}$")
_SCOPE_RE = re.compile(r"^(open-issues|open-prs|milestone=[1-9]\d{0,5}|label=[A-Za-z0-9._-]{1,50})$")
#: `SCAN <letter> <scope>: <reason>`; the first line of the prompt, uppercase.
LINE_RE = re.compile(r"^SCAN[ \t]+([A-Za-z])[ \t]+(\S+)[ \t]*:[ \t]*(.*)$")
ANY_RE = re.compile(r"^[ \t>]*scan[ \t]+[A-Za-z][ \t]", re.IGNORECASE)


def state_dir() -> Path:
    return SCAN_STATE_DIR if SCAN_STATE_DIR is not None else Path.home() / STATE_RELPATH


def _refuses_real_store() -> bool:
    if not ("unittest" in sys.modules or os.environ.get(TESTING_ENV) == "1"):
        return False
    try:
        return Path(state_dir()).resolve() == Path(REAL_SCAN_DIR).resolve()
    except OSError:
        return True


def parse(line: str) -> tuple[str, str, str] | str:
    """`(letter, scope, reason)` for a well-formed SCAN line, or a refusal reason."""
    match = LINE_RE.match(line or "")
    if match is None:
        return ("SCAN is honored only as the prompt's first line, typed unindented and "
                "uppercase, as `SCAN <letter> <scope>: <reason>`")
    letter, scope, reason = match.group(1).upper(), match.group(2), match.group(3).strip()
    if letter in CROSS_ACCOUNT:
        return (f"{letter} is another account with separate credentials; a scan from this "
                "session would return empty, not an error")
    if letter not in REPOS:
        return f"no repo prefix matches {letter!r}"
    if not _SCOPE_RE.match(scope):
        return (f"unknown scope {scope!r}: the scopes are open-issues, open-prs, "
                "milestone=<number> and label=<name>")
    if not reason:
        return "a SCAN line carries a reason after the colon"
    return letter, scope, reason


def _scope_name(scope: str) -> str:
    return hashlib.sha256(scope.encode()).hexdigest()[:16]


def grant_path(session: str, letter: str, scope: str) -> Path:
    return state_dir() / session / f"{letter.upper()}-{_scope_name(scope)}.json"


@contextlib.contextmanager
def _lock(path: Path):
    lock = path.with_suffix(".json.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def write(letter: str, scope: str, session: str, line: str, reason: str,
          now: datetime | None = None) -> dict:
    """The hook's write. A new SCAN line replaces any earlier grant for the same
    (session, letter, scope). Raises ValueError or OSError; the caller refuses."""
    letter = (letter or "").upper()
    if not (session and _SESSION_RE.match(session)) or letter not in REPOS \
            or not _SCOPE_RE.match(scope or ""):
        raise ValueError(f"session {session!r}, letter {letter!r} or scope {scope!r} "
                         "cannot name a grant")
    if _refuses_real_store():
        raise OSError("a test process never writes the real SCAN store")
    moment = now or imc_waiver._now()
    grant = {"letter": letter, "repo": REPOS[letter], "scope": scope, "session": session,
             "line": line, "reason": reason, "created_at": imc_waiver._iso(moment),
             "expires_at": imc_waiver._iso(moment + timedelta(hours=TTL_HOURS)),
             "hook_version": HOOK_VERSION}
    path = grant_path(session, letter, scope)
    with _lock(path):
        imc_waiver.write_json_atomic(path, grant)
    prune()
    return grant


def _letter_for(repo: str) -> str | None:
    return next((k for k, v in REPOS.items() if v.lower() == (repo or "").lower()), None)


def _live_grant(grant: dict | None, session: str, now: datetime) -> bool:
    if not grant or grant.get("session") != session:
        return False
    expiry = imc_waiver._parse_time(grant.get("expires_at"))
    return expiry is not None and now < expiry


def live(repo: str, scope: str, session: str, now: datetime | None = None) -> dict | None:
    """The live grant for exactly (repo, scope, session), or None. Never raises."""
    try:
        letter = _letter_for(repo)
        if letter is None or not session or not _SESSION_RE.match(session) \
                or _refuses_real_store():
            return None
        grant = imc_waiver.read_json(grant_path(session, letter, scope))
        if grant and grant.get("scope") == scope and _live_grant(grant, session,
                                                                  now or imc_waiver._now()):
            return grant
        return None
    except Exception:  # noqa: BLE001 - a grant that cannot be read unlocks nothing
        return None


def live_for_repo(repo: str, session: str, now: datetime | None = None) -> bool:
    """True when any live grant in this session names `repo`. The shim's check: it
    sees argv, not the scope, so it binds the repo; bounded_scan binds the scope."""
    try:
        letter = _letter_for(repo)
        if letter is None or not session or not _SESSION_RE.match(session) \
                or _refuses_real_store():
            return False
        moment = now or imc_waiver._now()
        for path in (state_dir() / session).glob(f"{letter}-*.json"):
            grant = imc_waiver.read_json(path)
            if grant and grant.get("repo", "").lower() == repo.lower() \
                    and _live_grant(grant, session, moment):
                return True
        return False
    except Exception:  # noqa: BLE001
        return False


def live_scopes(repo: str, session: str, now: datetime | None = None) -> list[str]:
    """Every scope with a live grant for `repo` in this session. The shim binds a
    call to one of these exact scopes (preclose pass 1, survivor 1): a grant for
    `label=foo` must never admit `state=all`. Never raises; [] on any doubt."""
    try:
        letter = _letter_for(repo)
        if letter is None or not session or not _SESSION_RE.match(session) \
                or _refuses_real_store():
            return []
        moment = now or imc_waiver._now()
        scopes = []
        for path in (state_dir() / session).glob(f"{letter}-*.json"):
            grant = imc_waiver.read_json(path)
            if grant and grant.get("repo", "").lower() == repo.lower() \
                    and _live_grant(grant, session, moment) \
                    and _SCOPE_RE.match(grant.get("scope", "")) \
                    and path == grant_path(session, letter, grant["scope"]):
                scopes.append(grant["scope"])
        return scopes
    except Exception:  # noqa: BLE001
        return []


def prune(now: datetime | None = None) -> None:
    """Drop grant and lock files untouched for `PRUNE_AFTER_DAYS` (by mtime), then
    any session directory left empty. Never raises."""
    if _refuses_real_store():
        return
    cutoff = (now.timestamp() if now is not None else time.time()) - PRUNE_AFTER_DAYS * 86400
    try:
        dirs = [d for d in state_dir().iterdir() if d.is_dir()]
    except OSError:
        return
    for directory in dirs:
        try:
            for path in directory.iterdir():
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
            directory.rmdir()
        except OSError:
            continue
