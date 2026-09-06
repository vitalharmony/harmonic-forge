#!/usr/bin/env python3
"""`PreToolUse` handler that records a compacted session's corpus re-read
(harmonic-forge#480) and enforces it (harmonic-forge#451).

WHAT THIS IS FOR
-------------------
`compaction_marker.py` records that a compaction happened. It cannot record
whether the session then acted on the injection it produced, because
`SessionStart` fires once and never sees a tool call. `PreToolUse` is the only
event that does -- and it is also the only event available to ENFORCE, since
no `PreCompact` hook exists to gate before or during a compaction.

F446 shipped detection and injection, and the salience test that motivated
#451 showed a session with the rule text already in context not acting on it.
Injection asks; this denies. Denial does not require the session's
cooperation, which is the whole argument for it.

WHY ONE HOOK AND NOT TWO
---------------------------
Recording and enforcing both need the same two facts -- the marker, and
whether THIS call is a corpus read -- so a sibling `PreToolUse` entry on the
same matcher would read the marker twice, compute `is_corpus_reload` twice,
and introduce an ordering dependency between two processes that must agree.
Ratified on #451.

WHY THE DETECTION LIVES IN THE OTHER MODULE
----------------------------------------------
`is_corpus_reload` and `CORPUS` are both in `compaction_marker.py`, imported
here. The corpus path list and the "what counts as reading one" list are one
fact in two halves, and that module's own docstring records what happens when
one fact gets a second hand-maintained copy (harmonic-forge#464). #451 imports
the same pair rather than a third copy.

WHY IT IS SAFE TO RUN ON EVERY TOOL CALL
-------------------------------------------
Two early exits before any parsing: no marker file for this session (the
common case -- most sessions never compact), and a marker already carrying
`reloaded_at`. Both are a stat and a small read.

THE ORDERING IS THE DESIGN (harmonic-forge#451)
--------------------------------------------------
    escape hatch set                    -> allow, and say so
    THIS CALL IS ITSELF A CORPUS RELOAD -> allow
    no marker / already reloaded        -> allow
    trigger is not "auto"               -> allow
    otherwise                           -> deny

**The second line is load-bearing and its absence is a permanent lockout.**
The deny fires on `PreToolUse`, and reading a corpus path IS a tool call --
so without an exemption a compacted session would be denied the exact reads
that clear the deny, on the first tool call of every enforced compaction,
told what to read and forbidden from reading it. `deny` rather than `ask`
makes that worse, not better: an `ask` a human could override, a `deny` they
cannot.

WHAT IT NEVER DOES
---------------------
It never CREATES a marker. A `PreToolUse` that could would manufacture a
compaction that never happened and then deny on it.

It never denies on a fact it did not establish. `trigger is None` means the
compaction record could not be read, not that it was automatic, and a
malformed marker fails OPEN with a visible message -- silence there would be
the harmonic-forge#440 failure, a gate that stopped working and told nobody.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from compaction_marker import (  # noqa: E402
    MARKER_DIR,
    corpus_for,
    is_corpus_reload,
    note_reload,
)

#: harmonic-forge#451 JDC3. Mirrors `model_tier_gate.py`'s `LANE_MODEL`
#: idiom: checked before anything else, and it announces itself rather than
#: silently disabling a gate.
ESCAPE_HATCH = "COMPACTION_GATE_OFF"


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}


def _read_marker(session_id: str) -> tuple[dict | None, str | None]:
    """`(marker, error)`. Both `None` means no marker -- never compacted."""
    path = MARKER_DIR / f"{session_id}.json"
    if not path.is_file():
        return None, None
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        return None, f"{type(err).__name__}: {err}"
    if not isinstance(marker, dict):
        return None, "marker is not an object"
    return marker, None


def handle(payload: dict, now: str | None = None,
           env: dict[str, str] | None = None) -> dict:
    """`{}` allows the tool call; a `permissionDecision` object denies it.

    See the module docstring for why the ordering below is the design.
    """
    env = os.environ if env is None else env
    if env.get(ESCAPE_HATCH):
        # Announced, not silent. A gate that can be switched off without
        # saying so is one nobody remembers is off.
        return {"systemMessage":
                f"compaction gate bypassed: {ESCAPE_HATCH} is set"}

    session_id = payload.get("session_id") or ""
    if not session_id:
        return {}

    # The marker is read BEFORE the command is parsed, and the order is a
    # cost decision rather than a logic one -- both branches allow, so no
    # verdict depends on it. Nearly every session never compacts, and F480
    # built this fast path deliberately: a `stat` on the hot path instead of
    # a `shlex` parse of every command in every session forever. Computing
    # the detector first (the shape this was first written in) silently
    # deleted that property, and F480's own test caught it.
    marker, error = _read_marker(session_id)
    if error is not None:
        # Fail OPEN, loudly (AC5). A gate that silently stops gating is
        # harmonic-forge#440; one that blocks on its own corrupt state is
        # worse, because the state it needs to clear itself is the state
        # that is broken.
        return {"systemMessage":
                f"compaction gate: marker for this session is unreadable "
                f"({error}); allowing the tool call rather than blocking on "
                f"a fact that could not be established"}
    if marker is None:
        return {}

    if is_corpus_reload(payload.get("tool_name") or "",
                        payload.get("tool_input") or {}):
        # BEFORE the deny, always. Without this the deny blocks the reads
        # that clear it -- see the module docstring.
        if not marker.get("reloaded_at"):
            note_reload(session_id, now or datetime.now(timezone.utc).isoformat())
        return {}

    if marker.get("reloaded_at"):
        return {}
    if marker.get("trigger") != "auto":
        # `manual` is the operator compacting deliberately (JDC2); `None`
        # means the compaction record could not be read. Neither is a fact
        # that justifies blocking work.
        return {}

    paths = corpus_for(str(marker.get("lane") or ""), str(marker.get("cwd") or ""))
    listed = "\n".join(f"  - {path}" for path in paths) or "  (none resolved)"
    return _deny(
        "This session was auto-compacted and has not re-read its protocol "
        "corpus since. Read at least one of these first -- that read is "
        "allowed and clears this block:\n"
        f"{listed}\n"
        f"Compacted at {marker.get('compacted_at')}. "
        f"Set {ESCAPE_HATCH}=1 to override.")


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        # Silent `{}` here, unlike `compaction_marker.main`, and the asymmetry
        # is deliberate: that hook's failure loses a session its recovery
        # note, which is worth a systemMessage. This one's failure loses a
        # flag nothing yet consumes, and a message on every malformed
        # PreToolUse payload would be noise on the hottest event there is.
        print("{}")
        return
    if not isinstance(payload, dict):
        print("{}")
        return
    print(json.dumps(handle(payload)))


if __name__ == "__main__":
    main()
