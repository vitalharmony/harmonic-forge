#!/usr/bin/env python3
"""`PreToolUse` guard: no raw `gh project item-list` (harmonic-forge#468).

WHY A HOOK AND NOT A COMMENT
-------------------------------
The module already said the right thing, in a comment:

    Use it whenever the question is "what is field X on issue N", and reserve
    fetch_item_list for questions that genuinely need the whole board.

and `repo_hygiene.py:498` independently called a full fetch "exactly the
quota-burn". **The GraphQL quota still went to zero twice with that guidance in
place** -- 2026-08-12 and 2026-09-04. A comment is not a mechanism.

WHY A BASH HOOK SPECIFICALLY
-------------------------------
Measured while planning this issue: `fetch_item_list` had **zero** production
callers in either repo, and every real consumer already used the cheap targeted
read. The two 5000-item cache files that named the 2026-09-04 incident were
written by an agent running ad-hoc Python -- code that does not exist until the
moment it runs, which no library default can reach in advance.

So the offender is a shell command, and this is the layer that sees it.

WHAT IS ALLOWED
------------------
`item_list_cache.py` itself, and anything invoking it, since that is the
mandated path. The check is on the *command*, not the caller: a `gh project
item-list` typed directly is denied; `python3 ... item_list_cache ...` is not
this hook's business.

`ask`, not `deny`: a genuine full-board question exists (drift checks, delta
syncs) and the operator can approve it. What must not happen is it going
unnoticed.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from shell_parse import command_segments, strip_invocation_prefix
except ImportError:  # pragma: no cover - the hook must never wedge a session
    command_segments = None
    strip_invocation_prefix = None

REASON = (
    "Raw `gh project item-list` is the full-board scan that zeroed the GraphQL "
    "quota on 2026-08-12 and again on 2026-09-04 (harmonic-forge#468). It costs "
    "hundreds of complexity points; GitHub bills complexity, not call count.\n\n"
    "Use the mandated path instead:\n"
    "  - one field on one issue  -> item_list_cache.fetch_issue_field(...)   (~1 point)\n"
    "  - the whole board, cached -> item_list_cache.get_board_items(...)\n"
    "  - a genuine full scan     -> item_list_cache.fetch_full_board(...)\n\n"
    "Approve this only if the question really needs every row live."
)


def is_raw_board_scan(command: str) -> bool:
    """True for a shell command that runs `gh project item-list` directly."""
    if not command or command_segments is None:
        return False
    for segment in command_segments(command):
        tokens = strip_invocation_prefix(segment)
        if len(tokens) >= 3 and tokens[0] == "gh":
            # `gh project item-list ...` — the subcommand pair, in order,
            # allowing global flags between them (`gh --repo x project ...`).
            rest = [t for t in tokens[1:] if not t.startswith("-")]
            if len(rest) >= 2 and rest[0] == "project" and rest[1] == "item-list":
                return True
    return False


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        # Visible, not silent: a malformed payload means the guard did not run,
        # and a quiet pass is indistinguishable from "nothing to flag" — the
        # harmonic-forge#440 failure posture this repo treats as the standard.
        print(json.dumps({"systemMessage":
                          "block_raw_board_scan: malformed hook payload; guard did not run"}))
        return
    if not isinstance(payload, dict):
        print(json.dumps({"systemMessage":
                          "block_raw_board_scan: hook payload was not an object"}))
        return

    command = ((payload.get("tool_input") or {}).get("command")) or ""
    if is_raw_board_scan(command):
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": REASON,
        }}))
        return
    print(json.dumps({}))


if __name__ == "__main__":
    main()
