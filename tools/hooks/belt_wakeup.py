#!/usr/bin/env python3
"""`SessionStart` wake-up for the belt-and-suspenders protocol (harmonic-forge#518 AC7).

WHAT THIS DOES, AND WHAT IT DELIBERATELY DOES NOT
----------------------------------------------------
It tells a lane session, at start and again after a compaction, which lane it is
and that `/belt-and-suspenders` exists. **It does not arm anything.**

That is an operator decision, stated plainly: auto-arming would make every
session start begin polling and spending, including throwaway sessions. Only the
wake-up is automatic. If this file ever grows a `Monitor(` call or a cron
create, that decision has been reversed by accident.

WHY IT IS A SECOND HOOK RATHER THAN MORE OF `compaction_marker.py`
--------------------------------------------------------------------
They fire on different matchers and answer different questions.
`compaction_marker.py` runs on `SessionStart` with `matcher: "compact"` and its
payload is *situational recovery* — what was lost, what to re-read. This runs on
every session start and its payload is *capability* — what this lane can arm.

The post-compaction case needs both, so `compaction_marker.build_context()`
carries one wake-up line as well. Two hooks, one line of overlap, rather than
one hook doing two jobs on two matchers.

Lane resolution is imported from `compaction_marker`, never re-implemented — a
second copy would drift, and the failure it drifts into is documented there: a
Lane 2 session told "You are LANE=1", which stopped and asked the operator
whether it was Lane 1. Twice.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from compaction_marker import resolve_lane  # noqa: E402

#: What each lane's belt is for, in one line. Not the protocol — the skill holds
#: that. This is only enough for a session to know the capability exists and
#: whether it is the right one to reach for.
_ROLE_LINE = {
    "1": "review, handoff, AE-and-sweep; merge and close after a PASS you re-verified live",
    "2": "implement from handoffs; never push, merge, close, or file issues",
    "3": "derive specs and execute gates; two-stage readiness, both stages every tick",
}


def build_wakeup(lane: str, source: str) -> str | None:
    """The injected line, or `None` when there is no lane.

    **No lane means no protocols, neither one** (settled decision). A session
    with no lane is not offered the skill, because the hazard being closed is
    exactly a no-lane session — the kind that files issues and writes handoffs —
    arming Lane 1's belt and moving work it has no authority to move.
    """
    if lane not in _ROLE_LINE:
        return None
    return (
        f"You are LANE={lane} ({source}). Your role in the belt-and-suspenders "
        f"loop: {_ROLE_LINE[lane]}.\n"
        "The belt (a 5-minute Monitor) and the suspenders (a 10-minute loop) are "
        "NOT armed — run `/belt-and-suspenders` to arm them. Arming is explicit "
        "so a throwaway session does not start polling and spending."
    )


def handle(payload: dict) -> dict:
    lane, source = resolve_lane(dict(os.environ), payload.get("cwd", os.getcwd()))
    context = build_wakeup(lane, source)
    if context is None:
        # Silent, not an error. A no-lane session is a normal thing to be.
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}
    out = handle(payload)
    if out:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
