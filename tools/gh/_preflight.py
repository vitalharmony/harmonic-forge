"""harmonic-forge#1076: a lane tool's first real run in a session needs its
preflight form first.

Lane 1 kept learning each tool's requirements from a refusal, and a written
note ("run it yourself minus the operator-only flag") did not bind. So each
guarded tool records a receipt when its preflight form runs (`--help`, or for
`preclose_check.py --force` the same command without `--force`) and refuses its
real run in a session that has no receipt for it. `l1_post.py`'s preflight form
is a clean `--dry-run` of the same post, keyed by `digest_key` to that post's
arguments (harmonic-forge#1115); `--help` no longer counts for it.

harmonic-forge#1215: every preflight is a read-only probe of the run's own
requirements, bound to that run's argument digest. The inventory:

| Step | Preflight |
|---|---|
| `l1_post.py` | a clean `--dry-run` of the same post (#1115) |
| `preclose_check.py --force` | the same command without `--force` (already a real run; exempt from the digest rule by name) |
| `lane1_rebase.py` | `--check <repo> <issue> <branch>`: `ls-remote`, no fetch, lock or prune; digest includes origin's branch SHA. `--help` unlocks nothing |
| `kill_check.py run` | `run --validate` with the same arguments: schema plus `git apply --check` on a throwaway index; digest includes the checks file |
| `cross_family_call.sh` | none separate: it validates its own arguments before any call |
| `mise run check` / `check-full` | none: no requirement beyond the tree; repeats are harmonic-forge#1216's |
| `gh pr merge` | none: pre-gated by `block_irreversible_ops.py` before it runs |
| `git push` of a TE branch | none here: harmonic-forge#1135's PreToolUse refusal |

The session is `CLAUDE_CODE_SESSION_ID`, the key `handoff_owed.session_id()`
already reads. An empty value (a non-Claude caller, CI) is not enforced: an
unidentified caller is never blocked. The id is hashed into the path, so no
value of it can name a directory outside the receipt store.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
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


def digest_key(key: str, digest: str) -> str:
    """harmonic-forge#1115: a receipt key bound to one specific run (an argument
    digest), so a preflight admits only the run it actually checked."""
    return f"{key}:{digest}"


def record(key: str) -> None:
    """Write this session's receipt for `key` (temp file plus rename, so two
    lanes writing at once never lose each other's receipts). No session, no
    receipt: the guarded run is not enforced either."""
    sid = session()
    if not sid:
        return
    path = _path(key, sid)
    # The preflight form itself is never gated, so a store error must never
    # break it: one try covers every filesystem step, and nothing in it raises
    # out. A `.tmp-` file a failed replace leaves behind is never read as a
    # receipt. The guarded run then refuses and names the store.
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"key": key, "recorded_at": time.time()}, fh)
        os.replace(tmp, path)
    except OSError as exc:
        if tmp:
            with contextlib.suppress(OSError):
                Path(tmp).unlink(missing_ok=True)
        print(f"[preflight] receipt not written ({exc}); the guarded run will refuse "
              f"until {receipt_dir()} is writable", file=sys.stderr)


def require(key: str, preflight_cmd: str) -> str | None:
    """None when the run may proceed, else the refusal naming `preflight_cmd`."""
    sid = session()
    if not sid or _path(key, sid).is_file():
        return None
    return (f"first real run of this tool in this session: run its preflight form first "
            f"and read what it requires (harmonic-forge#1076):\n  {preflight_cmd}\n"
            f"If you already ran it, the receipt store {receipt_dir()} is not writable "
            f"(see its [preflight] warning).")
