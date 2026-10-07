#!/usr/bin/env python3
"""`UserPromptSubmit` hook: the operator's typed `ALLOW EDIT` override for the
read-the-gate guard (harmonic-forge#918).

A line in the operator's own prompt starting `ALLOW EDIT` (optionally followed by
a reason) records a grant for the payload's `session_id` at
`~/.cache/harmonic-forge/ci_plan/<session_id>.allow`. `require_ci_plan.py` honors
it for the rest of the session. Active only when `LANE` is 1 or 2.

Same shape and provenance rules as `grant_loop_override.py` (`ALLOW LOOP`): the
candidate line comes from `directive_lines()` and the prompt must pass
`_provenance_refusal()`, so tool output, an injected envelope or a peer session's
message never mints a grant. Any internal failure records nothing.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lane"))
from ci_plan import allow_path  # noqa: E402
from expand_lane_shorthand import _provenance_refusal, directive_lines  # noqa: E402

LANES = ("1", "2")
_ALLOW_EDIT_RE = re.compile(r"^[ \t]*ALLOW EDIT\b(?P<reason>.*)$")


def find_grant_line(prompt: str) -> tuple[int, str] | None:
    for index, line in directive_lines(prompt):
        match = _ALLOW_EDIT_RE.match(line)
        if match:
            return index, match.group("reason").strip(" \t:-—")
    return None


def handle(payload: dict, lane: str | None, now: float | None = None) -> str | None:
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
        return f"ALLOW EDIT REFUSED: {refusal}. No grant was recorded."
    path = allow_path(payload.get("session_id"))
    if path is None:
        return "ALLOW EDIT REFUSED: the prompt carried no usable session_id. No grant was recorded."
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"created": time.time() if now is None else now,
                                "reason": reason}) + "\n", encoding="utf-8")
    return (f"ALLOW EDIT granted for LANE={lane}: the read-the-gate check is lifted for "
            "this session (harmonic-forge#918).")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        message = handle(payload, os.environ.get("LANE")) if isinstance(payload, dict) else None
    except Exception as exc:  # noqa: BLE001 - never cost the operator a message
        message = f"grant_ci_plan_override: no grant recorded ({type(exc).__name__}: {exc})"
    if message:
        print(json.dumps({
            "systemMessage": message,
            "hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                   "additionalContext": message},
        }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
