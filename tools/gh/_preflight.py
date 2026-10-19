"""harmonic-forge#1076: a lane tool's first real run in a session needs its
preflight form first.

Lane 1 kept learning each tool's requirements from a refusal, and a written
note ("run it yourself minus the operator-only flag") did not bind. So each
guarded tool records a receipt when its preflight form runs (`--help`, or for
`preclose_check.py --force` the same command without `--force`) and refuses its
real run in a session that has no receipt for it.

The session is `CLAUDE_CODE_SESSION_ID`, the key `handoff_owed.session_id()`
already reads. An empty value (a non-Claude caller, CI) is not enforced: an
unidentified caller is never blocked. The id is hashed into the path, so no
value of it can name a directory outside the receipt store.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

RECEIPT_DIR_ENV = "PREFLIGHT_RECEIPT_DIR"


def session() -> str:
    return os.environ.get("CLAUDE_CODE_SESSION_ID", "") or ""


def receipt_dir() -> Path:
    override = os.environ.get(RECEIPT_DIR_ENV)
    return Path(override) if override else Path.home() / ".claude" / "state" / "preflight"


def _path(key: str, sid: str) -> Path:
    session_dir = hashlib.sha256(sid.encode()).hexdigest()[:32]
    name = hashlib.sha256(key.encode()).hexdigest()[:32]
    return receipt_dir() / session_dir / f"{name}.json"


def record(key: str) -> None:
    """Write this session's receipt for `key` (temp file plus rename, so two
    lanes writing at once never lose each other's receipts). No session, no
    receipt: the guarded run is not enforced either."""
    sid = session()
    if not sid:
        return
    path = _path(key, sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"key": key, "recorded_at": time.time()}, fh)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def require(key: str, preflight_cmd: str) -> str | None:
    """None when the run may proceed, else the refusal naming `preflight_cmd`."""
    sid = session()
    if not sid or _path(key, sid).is_file():
        return None
    return (f"first real run of this tool in this session: run its preflight form first "
            f"and read what it requires (harmonic-forge#1076):\n  {preflight_cmd}")
