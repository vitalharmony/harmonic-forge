#!/usr/bin/env python3
"""PreToolUse hook: the sole, full-time gate for `gh issue close`/`gh pr
merge` (harmonic-forge#336, reforged after Lane 3's live gate FAIL).

`~/.claude/settings.json`'s `permissions.ask` no longer lists either
command class -- this hook decides both on every invocation, not just ones
covered by a live BATCH authorization. See `batch_auth.decide()`'s
docstring for the full rationale and the fail-toward-ask contract this
hook depends on absolutely: a silent exit here now means "not my command
class," never "I looked and it's fine."

Emits `allow` on a live, matching BATCH authorization; `ask` (explicitly,
never silently) on a covered command with no matching authorization, an
expired one, or anything this hook can't confidently classify; stays
silent only when the command isn't `gh issue close`/`gh pr merge` at all.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch_auth  # noqa: E402


def _emit(decision: str, reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        },
    }))


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return  # can't even read the payload -- nothing to classify, stay silent

    if payload.get("tool_name") != "Bash":
        return
    command = (payload.get("tool_input") or {}).get("command", "")
    if not command:
        return

    result = batch_auth.decide(command)
    if result is None:
        return  # not gh issue close / gh pr merge -- not this hook's decision
    decision, reason = result
    _emit(decision, reason)


if __name__ == "__main__":
    main()
