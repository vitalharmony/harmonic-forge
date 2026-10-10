#!/usr/bin/env python3
"""PreToolUse hook: deny a shell polling loop that has no timeout.

harmonic-forge#948. A Lane 1 subagent waited on its own background check with
`until grep -q "^exit=" <output>; do sleep 10; done`. The job died without
writing `exit=`, so the loop could never end, and the agent spun for about 57
minutes. A loop whose body sleeps (`until`/`while` ... `do` ... `sleep` ...
`done`) is denied unless a `timeout <N>` precedes it. A loop with no `sleep`
in its body (`while read line; do ...; done`) is not a poll and is allowed.
"""
from __future__ import annotations

import json
import re
import sys

LOOP = re.compile(r"(?s)\b(?:until|while)\b.*?(?:;|\n)\s*do\b(.*?)\bdone\b")
SLEEP = re.compile(r"\bsleep\b")
TIMEOUT = re.compile(r"\btimeout\s+\d")

REASON = (
    "Blocked: a polling loop (`until`/`while` with `sleep`) with no `timeout` "
    "can spin forever if what it waits for never happens (harmonic-forge#948: "
    "an agent spun 57 minutes on a dead job). Either wrap it, e.g. "
    "`timeout 1800 bash -c 'until ...; do sleep 10; done'`, or run the "
    "command in the background and rely on its completion notice."
)


def unbounded_poll(command: str) -> bool:
    """Whether the command holds a sleeping loop with no timeout before it."""
    for loop in LOOP.finditer(command):
        if SLEEP.search(loop.group(1)) and not TIMEOUT.search(command[:loop.start()]):
            return True
    return False


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        print(json.dumps({}))
        return
    command = (data.get("tool_input") or {}).get("command", "") if data.get("tool_name") == "Bash" else ""
    if command and unbounded_poll(command):
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": REASON,
            },
            "systemMessage": REASON,
        }))
        return
    print(json.dumps({}))


if __name__ == "__main__":
    main()
