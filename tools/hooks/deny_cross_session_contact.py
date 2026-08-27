#!/usr/bin/env python3
"""Deny `SendMessage` (cross-session/agent contact) unconditionally
(harmonic-forge#399).

Unlike every other hook in this directory, this one is not `LANE`-
conditional and does not parse a Bash command string — it denies by tool
name alone, for every `LANE` value including unset. The 3-lane protocol's
own rule (`feedback_never_cross_session_message_other_lanes`) is that
lanes coordinate through the GitHub issue thread, never by messaging each
other directly — a durable, auditable record beats a side-channel every
time, and there is no legitimate lane-to-lane use this hook needs to
carve out.

Content-blind by design (harmonic-forge#399's own AC3): an informational
message and an instructional one are denied identically. The distinction
this guard cares about is the *channel*, not the payload — a message that
would be perfectly fine posted as an issue comment is exactly as wrong
sent via `SendMessage`.

`matcher` in `.claude/settings.json` targets the tool name (`SendMessage`)
directly rather than `Bash` — this hook receives the full hook payload
and only needs `tool_name`, no command-shape parsing at all. Read-only
cross-session discovery (`ListAgents`) is a different tool name and is
therefore untouched by construction, not by an explicit carve-out.
"""

import json
import sys

DENIED_TOOL = "SendMessage"


def denial() -> dict:
    message = (
        "Blocked: SendMessage is denied for every session, regardless of "
        "LANE (harmonic-forge#399, feedback_never_cross_session_message_"
        "other_lanes). Lanes coordinate only through the GitHub issue "
        "thread -- post a comment there instead."
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": message,
        },
        "systemMessage": message,
    }


def decision(tool_name: object) -> dict:
    if tool_name == DENIED_TOOL:
        return denial()
    return {}


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        print("{}")
        return
    print(json.dumps(decision(payload.get("tool_name"))))


if __name__ == "__main__":
    main()
