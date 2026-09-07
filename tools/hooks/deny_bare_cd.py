#!/usr/bin/env python3
"""Deny a bare `cd <path> && ...` in the Bash tool (harmonic-forge#496, R-0349).

Promoted from `feedback_never_bare_cd_persistent_shell`, which recorded one
incident — but one that blocked a Lane 3 checkout machine-wide, which is why it
gets a mechanism rather than a line of prose. The Bash tool holds ONE persistent
shell for the session, so a bare `cd` relocates every later call in that
session, including calls made by a different task minutes later.

`git -C <dir>` and a subshell `(cd <dir> && cmd)` both do the job without
moving the session, so the correct forms are always available.

**Scope, deliberately narrow.** Only a command whose FIRST statement is a bare
`cd` is denied. A `cd` inside a subshell, inside `$(...)`, after `&&`, or in a
heredoc body is untouched — those do not relocate the session, and widening the
match would produce the false-positive rate that gets a hook disabled.
"""
import json
import re
import sys

#: Anchored at the start of the command, allowing leading whitespace. A leading
#: `(` is the subshell form and is explicitly the RECOMMENDED shape, so it must
#: never match here.
_BARE_CD = re.compile(r"^\s*cd\s+(?!-)\S")

_MESSAGE = (
    "Bare `cd` is denied (harmonic-forge#496, R-0349): the Bash tool holds one "
    "persistent shell for the whole session, so this relocates every later "
    "command — including ones a different task issues minutes from now. It has "
    "already blocked a Lane 3 checkout machine-wide.\n\n"
    "Use instead:\n"
    "  git -C <dir> <subcommand>        # for git\n"
    "  (cd <dir> && <cmd>)              # for anything else\n\n"
    "A `cd` inside a subshell or after `&&` is fine and is not matched."
)


def decision(command: str) -> dict:
    if not _BARE_CD.match(command or ""):
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
