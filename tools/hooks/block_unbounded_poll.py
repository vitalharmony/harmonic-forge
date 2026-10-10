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

Known limits, accepted (R-0380: no mechanism beyond the literal ask):
- denied although bounded: a `while read` loop over finite input that sleeps
  between items (AC1's literal rule), and any command holding both a loop and
  a separate `sleep`, before or after it;
- allowed although unbounded: a loop reached only through a wrapper before
  the shell (`env`/`nohup`/`nice … bash -c`), `eval`, a heredoc fed to a
  shell, or `timeout 0`. Each is a deliberate spelling, not the H2245 slip.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from shell_parse import command_segments  # noqa: E402

LOOP_WORDS = ("until", "while")
SHELLS = ("bash", "sh")
LEADERS = ("do", "then", "else", "elif", "{", "!", "command", "exec")


def _is_sleep(tokens: list[str]) -> bool:
    return bool(tokens) and Path(tokens[0]).name == "sleep"

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
        # Reforge pass 1: a loop or sleep may follow a compound keyword
        # (`{ until …`, `then while …`) or a builtin prefix (`command sleep`).
        while tokens and tokens[0] in LEADERS:
            tokens = tokens[1:]
        if not tokens:
            continue
        if tokens[0] in LOOP_WORDS:
            loop = True
            sleeps = sleeps or _is_sleep(tokens[1:])
        elif _is_sleep(tokens):
            sleeps = True
        elif (Path(tokens[0]).name in SHELLS and len(tokens) > 2
              and tokens[1].startswith("-") and tokens[1].endswith("c")
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
