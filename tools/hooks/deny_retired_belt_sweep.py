#!/usr/bin/env python3
"""Deny any Bash command that re-arms the retired account-wide belt sweep
(harmonic-forge#766).

harmonic-forge#640/#659 retired the sweep by operator ruling (it exhausted
the shared REST budget twice on 2026-09-14), but a session invoked it anyway
twelve days later: every surface that describes the retirement -- `--help`,
the old parse-time refusal, `DESIGN.md` -- kept the flag spelled out as a
runnable token, so a "never run X" in context left X available as a concrete
string to reach for. harmonic-forge#766 deletes the flag itself and every
spelled mention; this hook is the backstop for whatever transcript, stale
SKILL.md copy, or memory still names it.

**The message never echoes the token back.** A refusal that quotes the
command it forbids re-teaches it -- the exact mechanism this issue exists to
stop. Say only what the mechanism is and where to find the sanctioned
replacement.
"""
import json
import re
import sys

#: The retired flag's literal spelling, matched wherever it appears in a
#: command -- not anchored to the start, so `... && python3 watch_lane_posts.py
#: --sweep-for l1 ...` is caught the same as a bare invocation.
_RETIRED_SWEEP_FLAG = re.compile(r"--sweep-for\b")

_MESSAGE = (
    "The account-wide belt sweep is retired (harmonic-forge#640/#659). Arm "
    "only what `python3 ~/harmonic-forge/tools/lane/belt_plan.py` prints."
)


def decision(command: str) -> dict:
    if not _RETIRED_SWEEP_FLAG.search(command or ""):
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _MESSAGE,
        },
        "systemMessage": _MESSAGE,
    }


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        print("{}")
        return
    if payload.get("tool_name") != "Bash":
        print("{}")
        return
    print(json.dumps(decision((payload.get("tool_input") or {}).get("command", ""))))


if __name__ == "__main__":
    main()
