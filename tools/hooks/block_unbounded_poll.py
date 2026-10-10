#!/usr/bin/env python3
"""PreToolUse hook: deny a shell polling loop that has no timeout.

harmonic-forge#948. A Lane 1 subagent waited on its own background check with
`until grep -q "^exit=" <output>; do sleep 10; done`. The job died without
writing `exit=`, so the loop could never end, and the agent spun for about 57
minutes.

Reforged after preclose pass 1 (sticky-wicket REFORGE): the first version read
raw text with a regex, which could not see quoting or loop structure. This one
reads the shell segments from `shell_parse.command_segments`, the parser the
other hooks share, so quoted prose and heredoc bodies are never keywords.

Denied: a command whose top-level segments include an `until`/`while` loop and
a `sleep` (as its own segment, or as the loop's condition). A loop inside a
`bash -c`/`sh -c` payload is checked the same way, unless `timeout <N>` leads
that segment, which is the bounded form AC1 allows.

Known limits, accepted: a `while read` loop over finite input that sleeps
between items is denied (AC1's literal rule; the deny reason names the
fixes), and so is a loop followed by a separate `sleep` in the same command.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from shell_parse import command_segments  # noqa: E402

LOOP_WORDS = ("until", "while")
SHELLS = ("bash", "sh")

REASON = (
    "Blocked: a polling loop (`until`/`while` with `sleep`) with no `timeout` "
    "can spin forever if what it waits for never happens (harmonic-forge#948: "
    "an agent spun 57 minutes on a dead job). Either wrap it, e.g. "
    "`timeout 1800 bash -c 'until ...; do sleep 10; done'`, or run the "
    "command in the background and rely on its completion notice."
)


def unbounded_poll(command: str) -> bool:
    """Whether the command holds a sleeping until/while loop with no timeout."""
    try:
        segments = command_segments(command)
    except ValueError:  # unbalanced quoting: nothing to decide, allow
        return False
    loop = sleeps = False
    for tokens in segments:
        if tokens and tokens[0] == "do":
            tokens = tokens[1:]
        if not tokens:
            continue
        if tokens[0] in LOOP_WORDS:
            loop = True
            sleeps = sleeps or tokens[1:2] == ["sleep"]
        elif tokens[0] == "sleep":
            sleeps = True
        elif (Path(tokens[0]).name in SHELLS and len(tokens) > 2 and tokens[1] == "-c"
              and unbounded_poll(tokens[2])):
            return True
    return loop and sleeps


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        print(json.dumps({}))
        return
    command = ""
    if isinstance(data, dict) and data.get("tool_name") == "Bash":
        command = (data.get("tool_input") or {}).get("command", "")
    if isinstance(command, str) and command and unbounded_poll(command):
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
