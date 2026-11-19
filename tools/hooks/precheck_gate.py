#!/usr/bin/env python3
"""PreToolUse(Bash) entry for the precheck gate (harmonic-forge#1291).

Thin: reads the hook payload, calls `tools/precheck/gate.py`'s `decide`, and
prints a deny when it returns a reason. The decision and the decision log
live in gate.py; a gate that cannot load fails open (harmonic-forge#1291
sticky-wicket PATCH).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "precheck"))


def main() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - an unreadable payload decides nothing
        return 0
    try:
        import gate  # noqa: PLC0415
        reason = gate.decide(payload)
    except Exception:  # noqa: BLE001 - fail open: every gated tool's own consume() still refuses
        reason = None
    if reason:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
