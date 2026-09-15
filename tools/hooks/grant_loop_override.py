#!/usr/bin/env python3
"""`UserPromptSubmit` hook: the operator's typed `ALLOW LOOP` override for the
belt-arming guard (harmonic-forge#659, operator ruling 2026-09-14).

A line in the operator's own prompt starting `ALLOW LOOP` (optionally followed
by a reason) records a grant for the payload's `session_id` at
`<arming dir>/<session_id>.loop_grant`. `enforce_belt_arming.py` lets exactly
one non-canonical `/loop` + its `CronCreate` (or one bare `CronCreate`) through
under it, within `GRANT_TTL_SECONDS`, then consumes it. The belt `Monitor`
check is never affected. Active only when `LANE` is 1, 2 or 3.

WHY A SEPARATE FILE, NOT A UPS MODE IN enforce_belt_arming.py
-------------------------------------------------------------
The guard is a `PreToolUse` hook with one payload shape and one output shape
(`permissionDecision`). A second event mode would need event dispatch and two
output contracts in one `main()`. This file is ~the dispatch it would replace;
the grant format, TTL and path live in the guard and are imported from it, so
there is still one owner of the state.

PROVENANCE -- imported, not re-derived
--------------------------------------
The same rules as `BATCH` in `expand_lane_shorthand.py`: candidate lines come
from `directive_lines()` (outside fences, not quote-prefixed per
`_QUOTE_PREFIX_RE`), and the prompt must pass `_provenance_refusal()` -- which
refuses harness-injected envelopes (`<task-notification>`, a peer session's
message, slash-command text), a non-interactive entrypoint, and a line under an
emitter's "not an authorization" disclaimer. A refused `ALLOW LOOP` is said out
loud; nothing is recorded. Any internal failure records nothing (fail closed:
the operator re-types a line; the alternative is a self-minted override).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from enforce_belt_arming import GRANT_TTL_SECONDS, LANES, write_grant  # noqa: E402
from expand_lane_shorthand import _provenance_refusal, directive_lines  # noqa: E402

_ALLOW_LOOP_RE = re.compile(r"^[ \t]*ALLOW LOOP\b(?P<reason>.*)$")


def find_grant_line(prompt: str) -> tuple[int, str] | None:
    """`(line_index, reason)` of the first directive-position `ALLOW LOOP`."""
    for index, line in directive_lines(prompt):
        match = _ALLOW_LOOP_RE.match(line)
        if match:
            return index, match.group("reason").strip(" \t:-—")
    return None


def handle(payload: dict, lane: str | None, now: float | None = None) -> str | None:
    """The systemMessage to show, or None when the prompt has no grant line."""
    if lane not in LANES:
        return None
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        return None
    found = find_grant_line(prompt)
    if found is None:
        return None
    line_index, reason = found
    refusal = _provenance_refusal(prompt, line_index)
    if refusal is not None:
        return (f"ALLOW LOOP REFUSED: {refusal}. No grant was recorded; "
                "non-canonical /loop and CronCreate stay denied.")
    now = time.time() if now is None else now
    path = write_grant(payload.get("session_id"), reason, now=now)
    if path is None:
        return ("ALLOW LOOP REFUSED: the prompt carried no usable session_id. "
                "No grant was recorded.")
    expires = time.strftime("%H:%M:%S", time.localtime(now + GRANT_TTL_SECONDS))
    return (f"ALLOW LOOP granted for LANE={lane}: one non-canonical /loop (with "
            f"its CronCreate) or one CronCreate, until {expires} "
            f"({GRANT_TTL_SECONDS // 60} min). Belt Monitor unchanged "
            "(harmonic-forge#659).")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        message = handle(payload, os.environ.get("LANE")) if isinstance(payload, dict) else None
    except Exception as exc:  # noqa: BLE001 - never cost the operator a message
        message = f"grant_loop_override: no grant recorded ({type(exc).__name__}: {exc})"
    if message:
        print(json.dumps({
            "systemMessage": message,
            "hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                   "additionalContext": message},
        }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
