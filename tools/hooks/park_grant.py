#!/usr/bin/env python3
"""The PARK grant: the operator lets one open issue's last task-list entry
leave a lane's list once (harmonic-forge#1281, `rules/lane-shorthand.md`).

`PARK <KEY>` (or `PARK <KEY>: <reason>`), typed as the prompt's first line in
a LANE-set session, is honored by `expand_lane_shorthand.py` on
`UserPromptSubmit` (R-0117, the provenance BATCH, IMC and FORCE use). It
writes `<session>/<KEY>.json` under `PARK_STATE_DIR` (default
`~/.claude/state/park/`). `tasklist_hold.pretooluse_decide` asks `usable()`
at the point where it would deny removing an open issue's last entry, and
`consume()` spends the grant when it allows that removal.

A grant is bound to one session and one key, lasts `TTL_HOURS`, and is used
once. A consumed grant is kept, with `consumed_at`, as the audit record;
`prune` drops grant files untouched for `PRUNE_AFTER_DAYS`. Parking writes
nothing to GitHub (operator ruling 2026-11-07).

Like BATCH, IMC and FORCE, this trusts the operator-typed line and the
session: the grant file is an audit trail, not proof against a session that
writes this directory itself.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "gh"))
import imc_waiver  # noqa: E402  (time and atomic-write helpers, one copy)

#: `None` means `~/.claude/state/park`, resolved at call time, so a test that
#: patches this global or `HOME` never reaches the live store.
PARK_STATE_DIR: Path | None = None
STATE_RELPATH = Path(".claude") / "state" / "park"
#: The operator's real store, named once for the test guard (#907/#1257).
REAL_PARK_DIR = Path.home() / STATE_RELPATH
TESTING_ENV = "HARMONIC_FORGE_TESTING"
TTL_HOURS = 1.0
PRUNE_AFTER_DAYS = 7
HOOK_VERSION = "1"
#: A session id becomes a directory name; anything else is refused.
_SESSION_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}$")
_KEY_RE = re.compile(r"^[A-Z]\d{1,6}$")


def state_dir() -> Path:
    return PARK_STATE_DIR if PARK_STATE_DIR is not None else Path.home() / STATE_RELPATH


def _refuses_real_store() -> bool:
    """Whether this is a test process pointed at the operator's real store
    (`tasklist_owed._refuses_real_store`'s rule). Both paths are read at call
    time; an unresolvable path refuses rather than touches."""
    if not ("unittest" in sys.modules or os.environ.get(TESTING_ENV) == "1"):
        return False
    try:
        return Path(state_dir()).resolve() == Path(REAL_PARK_DIR).resolve()
    except OSError:
        return True


def _valid(session: str, key: str) -> bool:
    return bool(session and _SESSION_RE.match(session) and _KEY_RE.match(key or ""))


def grant_path(session: str, key: str) -> Path:
    return state_dir() / session / f"{key.upper()}.json"


@contextlib.contextmanager
def _lock(session: str, key: str):
    """Serialize one key's read-modify-write (FORCE's `grant_lock` shape)."""
    path = state_dir() / session / f"{key.upper()}.json.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def write(key: str, session: str, line: str, now: datetime | None = None) -> dict:
    """The hook's write. A new PARK line replaces any earlier grant for the
    (session, key). Raises OSError when it cannot write, or ValueError on an
    unusable session or key; the caller refuses."""
    key = (key or "").upper()
    if not _valid(session, key):
        raise ValueError(f"session {session!r} or key {key!r} cannot name a grant")
    if _refuses_real_store():
        raise OSError("a test process never writes the real PARK store")
    moment = now or imc_waiver._now()
    grant = {
        "key": key, "session": session, "line": line,
        "created_at": imc_waiver._iso(moment),
        "expires_at": imc_waiver._iso(moment + timedelta(hours=TTL_HOURS)),
        "consumed_at": None, "hook_version": HOOK_VERSION,
    }
    with _lock(session, key):
        imc_waiver.write_json_atomic(grant_path(session, key), grant)
    prune()  # by wall clock: file mtimes are wall clock, whatever `now` says
    return grant


def _usable(grant: dict | None, session: str, key: str, now: datetime) -> bool:
    if not grant or grant.get("session") != session or grant.get("key") != key:
        return False
    if grant.get("consumed_at") is not None:
        return False
    expiry = imc_waiver._parse_time(grant.get("expires_at"))
    return expiry is not None and now < expiry


def usable(session: str, key: str, now: datetime | None = None) -> bool:
    """True when an unconsumed, unexpired grant for exactly (session, key)
    exists. Never raises; an unreadable or invalid file is not usable."""
    try:
        key = (key or "").upper()
        if not _valid(session, key) or _refuses_real_store():
            return False
        grant = imc_waiver.read_json(grant_path(session, key))
        return _usable(grant, session, key, now or imc_waiver._now())
    except Exception:  # noqa: BLE001 - a grant that cannot be read unlocks nothing
        return False


def consume(session: str, key: str, now: datetime | None = None) -> dict:
    """Recheck `usable` under the key's lock and mark the grant consumed.
    Raises OSError when the grant is not usable or cannot be rewritten."""
    key = (key or "").upper()
    if not _valid(session, key) or _refuses_real_store():
        raise OSError(f"no usable PARK grant for {key} in this session")
    moment = now or imc_waiver._now()
    with _lock(session, key):
        path = grant_path(session, key)
        grant = imc_waiver.read_json(path)
        if not _usable(grant, session, key, moment):
            raise OSError(f"no usable PARK grant for {key} in this session")
        grant["consumed_at"] = imc_waiver._iso(moment)
        imc_waiver.write_json_atomic(path, grant)
    return grant


def prune(now: datetime | None = None) -> None:
    """Drop grant and lock files untouched for `PRUNE_AFTER_DAYS` (by mtime),
    then any session directory left empty. Never raises."""
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
