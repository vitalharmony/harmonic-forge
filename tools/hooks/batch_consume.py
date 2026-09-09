#!/usr/bin/env python3
"""PostToolUse hook: record BATCH consumption AFTER the command actually ran.

harmonic-forge#552, AC2. This is the write half that `batch_auth.decide()`
used to perform at `PreToolUse` time, which was the root cause of the incident
that filed #552.

## Why the write cannot live in the decision

`decide()` is a `PreToolUse` function. It cannot know whether the command will
run, for three separate reasons, and all three happened live:

| the command…                              | executes? | slot consumed (before)? |
|---|---|---|
| is denied by another PreToolUse hook      | no        | **yes** |
| is declined by the operator at the prompt | no        | **yes** |
| runs and fails (not mergeable, conflict)  | no merge  | **yes** |

Claude Code composes `PreToolUse` hooks under strongest-decision-wins, so a
sibling hook's `deny` lands *after* the state has already been written. On
2026-09-09 that produced four consumed merge targets on one key, every one
pointing at PR 1753 — which was still unmerged. Four matches, zero executions.
Each operator prompt consumed another slot, so every retry started worse off.

`PostToolUse` fires only once the tool has executed, which removes the first
two paths by construction. The third is removed by `action_landed()`: this hook
consumes only after confirming the PR is actually merged (or the issue actually
closed), because `PostToolUse` fires on failure too and consuming
unconditionally here would merely move the over-count rather than fix it.

## Fail-direction, and why it is the opposite of decide()'s

`decide()` fails **closed** — an unresolvable command asks. This hook fails
**open**: any error, any ambiguity, any unreachable API leaves the grant live
and consumes nothing. The asymmetry is deliberate and the costs are asymmetric.
A grant left live costs one extra prompt on the next command. A grant wrongly
consumed costs a stuck batch and an operator diagnosing hook state by hand —
which is exactly the failure this issue exists to end.

This hook therefore never blocks, never denies, and never raises into the
session. It emits nothing on success; `PostToolUse` output would be noise on
every merge.
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
        return  # cannot read the payload — nothing to record, stay silent

    if payload.get("tool_name") != "Bash":
        return
    command = (payload.get("tool_input") or {}).get("command", "")
    if not command:
        return

    try:
        batch_auth.consume(command)
    except Exception:
        # Fail open, loudly to nobody: a bookkeeping miss is one extra prompt.
        return


if __name__ == "__main__":
    main()
