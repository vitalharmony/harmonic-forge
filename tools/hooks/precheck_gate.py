#!/usr/bin/env python3
"""PreToolUse(Bash) entry for the precheck gate (harmonic-forge#1291).

Thin: reads the hook payload, calls `tools/precheck/gate.py`'s `decide`, and
prints a deny when it returns a reason. The decision, the fail-open/closed
rule and the decision log all live in gate.py.
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
    except Exception as exc:  # noqa: BLE001 - the gate's import failed: fail closed only where needed
        reason = _import_failure(payload, exc)
    if reason:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}))
    return 0


#: The three precheck tools with no tool-side backstop (registry `backstop = false`).
_UNBACKSTOPPED = ("repo_hygiene", "lanes-claude", "check_lane3_ready", "lane3-begin")


def _import_failure(payload: object, exc: Exception) -> str | None:
    command = ""
    if isinstance(payload, dict) and isinstance(payload.get("tool_input"), dict):
        command = str(payload["tool_input"].get("command") or "")
    if any(name in command for name in _UNBACKSTOPPED):
        return (f"precheck gate (harmonic-forge#1291) could not load ({type(exc).__name__}); this "
                "command names a gated tool with no tool-side backstop, so it is denied.")
    return None


if __name__ == "__main__":
    raise SystemExit(main())
