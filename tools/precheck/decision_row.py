#!/usr/bin/env python3
"""One row shape for `decisions.jsonl` (harmonic-forge#1448).

The gate (`gate.log`), the tool-side backstop (`gate_receipt.consume`) and the
post-run recorder (`tools/hooks/record_precheck_outcome.py`) all write rows
through here, so the three writers cannot drift apart. A row is ts, outcome,
tool, mode, class, fp (12 chars), session (hashed), tuid (hashed
`tool_use_id`), exit, reason. Never an argv token.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
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
