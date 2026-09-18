#!/usr/bin/env python3
"""Stop hook: refuse to end a turn with a handoff still owed (harmonic-forge#687).

R-0039 makes filing an issue and posting its handoff **one atomic action**.
forge#516 merged that text; nothing enforced it. The finding that produced
this hook is hrse#1919, 2026-09-17: an issue filed with a substantive body
and no handoff, by a session that had read R-0039's full text aloud minutes
earlier in order to apply its three tests. It was caught only because the
operator asked *"does 1919 have a handoff?"*

So the rule was present, correctly worded, recently read and consciously
applied -- and the split still happened, because the tooling made the
incomplete state indistinguishable from the complete one. That is the
argument `block_missing_preclose_inspection.py` already states for this
repo, verbatim:

> Every other closure-adjacent rule that has actually held under pressure
> in this repo (auto-close keyword syntax, Lane 1/2 status claims, AE
> self-post, the data-migration close gate this hook is modeled on) is
> enforced by a PreToolUse hook that blocks the Bash call outright.

A handoff is not closure-adjacent, so this is a **Stop** hook rather than
a PreToolUse one -- the obligation is not attached to any single later
command, it is attached to the turn ending. That shape follows HRSE2's
`scripts/enforce_sprint_plan_summary.py`.

## Positive signal only -- AC6 is satisfied by construction

The obligation exists **only because `gh_issue.py` wrote it**. An issue
created by `gh api`, the web UI, or any other route leaves no record and
therefore cannot block anything.

That direction is not a convenience. `block_missing_preclose_inspection.py`
was corrected into it after two implementations gated on the *absence* of a
signal, and its docstring says what that cost:

> The only escape was applying `preclose-inspected`, which falsely asserts
> a review that never ran and corrodes the one signal the hook depends on.
> **A gate whose escape hatch is a lie is worse than no gate.**

The same reasoning is why this hook's exception route (`--handoff-exception`
on `gh_issue.py`) writes its reason to the **issue** and not to local state:
taking the escape hatch leaves a durable public record instead of a private
claim, so using it honestly costs the same as using it dishonestly.

## Why the message names all three discharge routes

The block stands until the handoff posts, and `l1_post.py` validates
handoff shape and is right to. A session that gets its handoff rejected
would otherwise be stuck retrying against a validator that keeps saying no,
with the escape hatch available but unmentioned at the one moment it is
needed. AC1 requires the `l1-post` command; naming the other two routes as
well is treating that as a floor, not a ceiling.

## It cannot wedge a session

`stop_hook_active` is honoured first: this hook's condition IS clearable
within the turn (post the handoff, claim the exception), but a session that
somehow cannot clear it must still be able to end. Beyond that, every
failure path returns "allow" silently -- an unreadable store, a missing
session id, a malformed payload. Missing a stop is recoverable; wedging a
session into never ending its turn is not.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from handoff_owed import discharge_command, outstanding  # noqa: E402


def _describe(entry: dict) -> str:
    repo = str(entry.get("repo") or "")
    issue = entry.get("issue")
    url = str(entry.get("url") or "")
    head = f"{repo}#{issue}" + (f" — {url}" if url else "")
    try:
        command = discharge_command(repo, int(issue))
    except (TypeError, ValueError):
        return f"  - {head}"
    return f"  - {head}\n      {command}"


def decide(payload: dict) -> tuple[str, str]:
    """`("allow", "")` or `("block", reason)`."""
    key = payload.get("session_id") or ""
    if not key:
        # No key means no way to attribute an obligation to this turn.
        # Blocking on someone else's record is the one outcome worse than
        # missing this one.
        return "allow", ""
    owed = [e for e in outstanding(key) if e.get("repo") and e.get("issue")]
    if not owed:
        return "allow", ""
    plural = "issue" if len(owed) == 1 else "issues"
    listing = "\n".join(_describe(e) for e in owed)
    return "block", (
        f"You filed {len(owed)} {plural} this turn and have not posted a "
        f"handoff for {'it' if len(owed) == 1 else 'them'}.\n\n"
        f"R-0039: filing an issue and posting its handoff are ONE action. "
        f"An un-handoffable issue is a failed filing-bar test — the split "
        f"state you are in now is not a partial success, it is the failure "
        f"mode the rule names.\n\n"
        f"Owed:\n{listing}\n\n"
        f"Three ways to discharge this, and only these:\n"
        f"  1. Post the handoff with the command shown above.\n"
        f"  2. If R-0039's filing bar genuinely exempts it, re-run with "
        f"`--handoff-exception <parent-epic|deferred-design-record|"
        f"blocked-on-prerequisite> --handoff-exception-reason \"<why>\"`. "
        f"The reason is posted to the issue, not kept locally.\n"
        f"  3. If the issue was NOT actually created, the record is stale — "
        f"say so and it can be cleared by hand from "
        f"`~/.cache/harmonic-forge/handoff_owed/`.\n\n"
        f"Do not end the turn by explaining that the handoff is coming next."
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    # Claude Code's documented recursion guard, honoured before anything else.
    if not isinstance(payload, dict) or payload.get("stop_hook_active"):
        return 0
    try:
        verdict, reason = decide(payload)
    except Exception:
        return 0
    if verdict == "block":
        print(json.dumps({"decision": "block", "reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
