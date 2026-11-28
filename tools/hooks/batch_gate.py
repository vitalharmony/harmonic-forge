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

harmonic-forge#1663 (operator D90, D93): `main` arms
`manifest_overlay.refuse_reads` first, so nothing this process runs may read an
app repo's overlay. A refused read is a BaseException, which `batch_auth.decide`'s
fail-toward-ask `except Exception` does not catch; `main` catches it and emits
`ask`, never a silent non-zero exit (that would leave the call to the session's
permission mode). An overlay module that cannot be imported, so cannot be armed,
also asks on a covered command.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch_auth  # noqa: E402

# harmonic-forge#1663: at module level, so the arming call stays main's first
# statement (it was on the path only as a side effect of batch_auth's import).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "onboard"))


class _Unarmable:
    """Stands in for `manifest_overlay` when it cannot be imported: arming is a
    no-op, and `main` asks on every covered command (pitch change 2)."""

    class OverlayReadRefused(BaseException):
        pass

    def __init__(self, error: BaseException) -> None:
        self.error = f"{type(error).__name__}: {error}"

    def refuse_reads(self, reason: str) -> None:
        pass


try:
    import manifest_overlay  # noqa: E402
except Exception as _exc:  # noqa: BLE001 -- never a silent crash: main asks instead
    manifest_overlay = _Unarmable(_exc)

ASK_REFUSED = ("Overlay read refused in a grant-writing hook (harmonic-forge#1663): {detail}. "
               "batch_gate may not read an app repo's .claude/forge.toml, so it cannot "
               "decide this command -- failing closed to an explicit ask.")


def _emit(decision: str, reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        },
    }))


def main() -> None:
    manifest_overlay.refuse_reads("batch_gate.main")
    try:
        _gate()
    except manifest_overlay.OverlayReadRefused as exc:
        _emit("ask", ASK_REFUSED.format(detail=exc))


def _gate() -> None:
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
    if isinstance(manifest_overlay, _Unarmable):
        _emit("ask", ASK_REFUSED.format(
            detail=f"the overlay module could not be imported to arm the refusal "
                   f"({manifest_overlay.error})"))
        return
    decision, reason = result
    _emit(decision, reason)


if __name__ == "__main__":
    main()
