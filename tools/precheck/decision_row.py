#!/usr/bin/env python3
"""One row shape for `decisions.jsonl` (harmonic-forge#1448).

The gate (`gate.log`), the tool-side backstop (`gate_receipt.consume`) and the
post-run recorder (`tools/hooks/record_precheck_outcome.py`) all write rows
through here, so the three writers cannot drift apart. A row is ts, outcome,
tool, mode, class, fp (12 chars), session (hashed), tuid (hashed
`tool_use_id`), exit, reason. Never an argv token.

Each gate row with a tuid is also written to `<store>/calls/<session>-<tuid>.json`
(`record_gate`), so the recorder finds its row by name, never by scanning the
shared log, whatever its size (harmonic-forge#1448 sticky-wicket D).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path


def digest(value: str | None) -> str | None:
    return hashlib.sha256(value.encode()).hexdigest()[:16] if value else None


def row(outcome: str, *, tool: str | None = None, mode: str | None = None, klass: str | None = None,
        fp: str | None = None, sid: str = "", tuid: str | None = None, exit: int | None = None,
        reason: str | None = None, now: float | None = None, hashed: bool = False) -> dict:
    """`hashed`: `sid` and `tuid` are already digests (the recorder copies them
    from the gate row it joins)."""
    return {"ts": time.time() if now is None else now, "outcome": outcome, "tool": tool,
            "mode": mode, "class": klass, "fp": fp[:12] if fp else None,
            "session": (sid or None) if hashed else digest(sid),
            "tuid": (tuid or None) if hashed else digest(tuid),
            "exit": exit, "reason": reason}


def append(path: Path, data: dict) -> None:
    """Append one row; a store problem is never surfaced."""
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(data, sort_keys=True) + "\n")


#: Call files older than this are pruned by the recorder.
CALL_TTL = 24 * 3600


def call_path(store: Path, session: str | None, tuid: str | None) -> Path | None:
    """`<store>/calls/<session-digest>-<tuid-digest>.json`; None without both."""
    if not session or not tuid:
        return None
    return store / "calls" / f"{session}-{tuid}.json"


def write_call(store: Path, data: dict) -> None:
    """Atomically write the per-call file for a gate row. An existing
    allowed-receipt file is never overwritten by a later non-receipt row, so a
    receipted call keeps its attribution. A store problem is never surfaced."""
    with contextlib.suppress(OSError, ValueError, TypeError):
        path = call_path(store, data.get("session"), data.get("tuid"))
        if path is None:
            return
        if data.get("outcome") != "allowed-receipt":
            with contextlib.suppress(OSError, ValueError):
                prior = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(prior, dict) and prior.get("outcome") == "allowed-receipt":
                    return
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".call.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, sort_keys=True)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise


def record_gate(store: Path, data: dict) -> None:
    """A gate decision: one log row plus its per-call file. Never raises OSError."""
    append(store / "decisions.jsonl", data)
    write_call(store, data)


def prune_calls(store: Path, now: float | None = None) -> None:
    """Delete call files (and stray temp files) older than CALL_TTL."""
    cutoff = (time.time() if now is None else now) - CALL_TTL
    with contextlib.suppress(OSError):
        with os.scandir(store / "calls") as entries:
            for entry in entries:
                with contextlib.suppress(OSError):
                    if entry.is_file() and entry.stat().st_mtime < cutoff:
                        os.unlink(entry.path)
