#!/usr/bin/env python3
"""Deny a plain-text Gmail draft (harmonic-forge#950).

`draft_gmail_message` defaults to `body_format: "plain"`. Gmail opens a
`text/plain` draft in plain-text compose mode and hard-wraps it at about 70
columns when the operator sends it, and a hard-wrapped email reads as
machine-written. An html draft is stored as `multipart/alternative` and is not
wrapped. An operator memory rule asking for html failed twice, so this is a
PreToolUse hook instead.

Matches by tool-name tail, so it covers every workspace server
(`mcp__workspace-vh__…`, `mcp__workspace-kenekted__…`, any future one), the
same idea as `~/.claude/hooks/deny_outbound_send.py`.

**Fails open.** The harm is cosmetic; blocking all drafting on a malformed
payload would be worse. Any parse error or unexpected shape allows the call.
"""
from __future__ import annotations

import json
import sys

TOOL_TAIL = "draft_gmail_message"
REASON = ('Gmail hard-wraps plain-text drafts at send (harmonic-forge#950). '
          'Pass body_format: "html", one <p>…</p> per paragraph.')


def decide(payload: object) -> str | None:
    """The deny reason for this payload, or None to allow."""
    if not isinstance(payload, dict):
        return None
    name = payload.get("tool_name")
    if not isinstance(name, str) or not name.endswith(TOOL_TAIL):
        return None
    tool_input = payload.get("tool_input")
    if tool_input is None:
        tool_input = {}  # missing: the tool's default (plain) applies, so it is checked
    if not isinstance(tool_input, dict):  # malformed, including a falsy [] or "": fail open
        return None
    # The tool's own default is "plain", so a missing body_format is denied too.
    return None if tool_input.get("body_format") == "html" else REASON


def main() -> None:
    try:
        reason = decide(json.loads(sys.stdin.read()))
    except Exception:  # noqa: BLE001 -- fail open, see the module docstring
        sys.exit(0)
    if reason:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }}))
    sys.exit(0)


if __name__ == "__main__":
    main()
