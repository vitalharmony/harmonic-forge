#!/usr/bin/env python3
"""PreToolUse hook: allow a Bash command covered by a live BATCH
authorization (harmonic-forge#336).

Sibling to `block_irreversible_ops.py`'s own `batch_auth.check_and_consume()`
call -- that hook silences its own `ask` for a covered command; this hook is
the one that actually emits `permissionDecision: allow` against
`permissions.ask` (`gh issue close *` / `gh pr merge *` in
`~/.claude/settings.json`). Consumption is idempotent per command hash (see
`batch_auth.py`), so it does not matter which of the two hooks runs first.

Stays silent (no output) for anything it does not recognize as a live,
matching authorization -- a non-match is never this hook's decision, and the
normal permission flow (allow / ask / deny) applies unchanged.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch_auth  # noqa: E402


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return  # unparseable input: stay silent, let other layers decide

    if payload.get("tool_name") != "Bash":
        return
    command = (payload.get("tool_input") or {}).get("command", "")
    if not command:
        return

    matched, reason = batch_auth.check_and_consume(command)
    if not matched:
        return

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": reason,
        },
    }))


if __name__ == "__main__":
    main()
