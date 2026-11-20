#!/usr/bin/env python3
"""One row shape for `decisions.jsonl` (harmonic-forge#1448).

The gate (`gate.log`), the tool-side backstop (`gate_receipt.consume`) and the
post-run recorder (`tools/hooks/record_precheck_outcome.py`) all write rows
through here, so the three writers cannot drift apart. A row is ts, outcome,
tool, mode, class, fp (12 chars), session (hashed), tuid (hashed
`tool_use_id`), exit, reason. Never an argv token.

Each gate row with a tuid is also written to `<store>/calls/<session>-<tuid>.json`,
or `.receipt.json` for an allowed-receipt row (`record_gate`), so the recorder finds its row by name, never by scanning the
shared log, whatever its size (harmonic-forge#1448 sticky-wicket D).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
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


#: A call file larger than this is treated as absent, never parsed.
CALL_MAX_BYTES = 64 * 1024


def read_call(path: Path) -> dict | None:
    """A call file's row, or None for a file that is absent, oversized, not a
    regular file, or unreadable for any reason (a 10,000-deep nest raises
    RecursionError). Used by the recorder only, never by the gate: the gate path
    does no reads (sticky-wicket F1448 epoch 2). The size is checked with lstat
    before any open; the read is a single bounded `os.read`, non-blocking and
    never following a symlink, so a planted FIFO or huge file never blocks."""
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > CALL_MAX_BYTES:
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        try:
            raw = os.read(fd, CALL_MAX_BYTES + 1)
        finally:
            os.close(fd)
        if len(raw) > CALL_MAX_BYTES:
            return None
        prior = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001 -- includes RecursionError and MemoryError
        return None
    return prior if isinstance(prior, dict) else None


def receipt_path(path: Path) -> Path:
    """`<s>-<t>.receipt.json` beside `<s>-<t>.json`."""
    return path.with_name(path.name[: -len(".json")] + ".receipt.json")


def write_call(store: Path, data: dict) -> None:
    """Write the per-call file for a gate row without reading anything.

    An allowed-receipt row goes to `<s>-<t>.receipt.json`: written to a temp
    file, then `os.link`ed into place, so the kernel makes the first receipt win
    (a later one gets EEXIST and is dropped) and a crash never leaves an empty
    receipt. Any other row replaces `<s>-<t>.json` atomically. A store problem
    is never surfaced."""
    with contextlib.suppress(Exception):
        path = call_path(store, data.get("session"), data.get("tuid"))
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".call.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, sort_keys=True)
            if data.get("outcome") == "allowed-receipt":
                with contextlib.suppress(FileExistsError):
                    os.link(tmp, receipt_path(path))
            else:
                os.replace(tmp, path)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


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
