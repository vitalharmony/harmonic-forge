#!/usr/bin/env python3
"""PostToolUse nudge: remind Lane 1 to post the Gate-readiness sweep
immediately after approving a Lane 3 test spec (hrse#389/harmonic-forge#134).

Prose documentation of this requirement already exists in two places
(.devin/skills/lane3-gate/SKILL.md, AGENTS.md) and has failed twice in one
session (hrse#431, then hrse#429/#430/#432/#433/#434 all at once) --
matching this project's own repeated finding that prose alone doesn't hold
for a step easy to skip under momentum. This is a reminder, not a hard
block: PreToolUse-level enforcement isn't feasible here (the hook can't
know a spec was approved without reading posted-comment history, and
denying the comment itself would be the wrong failure mode -- the review
content is still valid, only the follow-up step is missing).
"""

import json
import re
import shlex
import sys
from pathlib import Path

SPEC_REVIEW_MARKER = re.compile(r"Lane 1 test spec review", re.IGNORECASE)


def find_file_arg(command: str) -> str | None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    for index, token in enumerate(tokens):
        if token == "--file" and index + 1 < len(tokens):
            return tokens[index + 1]
        if token.startswith("--file="):
            return token.partition("=")[2]
    return None


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        print("{}")
        return
    command = (payload.get("tool_input") or {}).get("command", "")
    if "mise run lane-comment" not in command and "post_lane_discussion.py" not in command:
        print("{}")
        return
    file_arg = find_file_arg(command)
    if not file_arg:
        print("{}")
        return
    try:
        content = Path(file_arg).expanduser().read_text(encoding="utf-8")
    except OSError:
        print("{}")
        return
    if not SPEC_REVIEW_MARKER.search(content):
        print("{}")
        return
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": (
                "This comment approved a Lane 3 test spec. Per hrse#389, the "
                "Gate-readiness sweep is the mandatory next step -- post it "
                "now, in this same turn, via `mise run l1-post --kind sweep "
                "--spec-comment <id>`, before ending the turn or moving to "
                "another issue. This has been missed twice already this "
                "session (hrse#431, then hrse#429/#430/#432/#433/#434) -- "
                "do not let it happen a third time."
            ),
        }
    }))


if __name__ == "__main__":
    main()
