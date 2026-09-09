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

WHICH SESSIONSTART SOURCES THIS MATCHES, AND WHY (harmonic-forge#560)
-----------------------------------------------------------------------
`startup|resume|clear|fork`. This is the settled answer to #560's two candidate
mechanisms, and the next person changing this string should know which one they
are working against:

**BOTH mechanisms are live, and the first draft of this note recorded only the
first one — wrongly.** Read the CLI itself (2.1.267) rather than inferring:

  * `clear` IS a distinct `SessionStart` source, so a `startup|resume` matcher
    never fired on it. That is mechanism 1, and adding `clear` fixes it.
  * AND `/clear` discards an injection that already happened. `clearConversation`
    releases the deferred startup hooks, then empties the message list. So
    `lane<N> /clear` — which is `claude /clear`, an ordinary `startup` session
    followed by the command — DID fire at `startup` and DID have its injection
    wiped. That is mechanism 2, and it is why the matcher change is necessary
    rather than merely tidy: the re-fire on `clear` is the only injection that
    survives, because `clearConversation` sets the cleared conversation's
    messages FROM the clear-source hook results.

Getting this backwards matters for the next person: "mechanism 1 only" implies
`/clear` preserves injections, which is exactly the belief that makes dropping
`clear` from the matcher look safe.

The full source vocabulary is `startup`, `resume`, `clear`, `compact`, `fork`.
`fork` is included here for the same reason `clear` is: it starts a session that
needs to know its lane, and excluding it would rebuild this exact bug for a
different launch path. `compact` is deliberately EXCLUDED — it has its own
entry running `compaction_marker.py`, whose `build_context()` already carries a
wake-up line, and matching it here would inject twice.

The cost of the gap is measured, not hypothetical. A fresh Lane 1 session
started this way opened by asserting "no LANE is set in this session, so I'm not
operating as Lane 1/2/3" — with `LANE=1` set in its environment — and then
compounded it by inventing a trigger phrase for itself.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
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


#: Every fire, appended. harmonic-forge#560's acceptance test is "ask a fresh
#: session what told it its lane" — a question whose answer is a session's
#: self-report, which is exactly the kind of evidence this platform has learned
#: not to trust. This log answers it mechanically instead, and it is what
#: separates the issue's two candidate mechanisms:
#:
#:   * a `clear` line AND a session that knows its lane -> working as intended.
#:   * a `startup` line but no `clear` line for a `lane<N> /clear` launch -> the
#:     matcher does not cover `clear`; the startup injection was wiped by
#:     `/clear` and nothing replaced it. This is the shipped defect's signature.
#:
#: Note what it is NOT: "no line at all". An earlier draft of this comment said
#: a `/clear` launch would produce no line if the matcher were wrong, which is
#: false — `lane<N> /clear` is a `startup` session plus a command, so the
#: startup fire always happens. An operator applying that rule to real data
#: would have landed on neither branch and read the log as evidence against the
#: very mechanism it was recording.
#:
#: Evidence only. Nothing reads this to make a decision, and a logging failure
#: can never stop a session from starting.
FIRE_LOG = Path.home() / ".claude" / "state" / "belt-wakeup-fires.jsonl"
FIRE_LOG_MAX = 500


def record_fire(payload: dict, lane: str | None, injected: bool,
                path: Path | None = None) -> bool:
    """Append one fire record. Returns False on any failure, never raises."""
    target = path or FIRE_LOG
    try:
        entry = {
            "at": datetime.now(timezone.utc).isoformat(),
            # The SessionStart SOURCE — "startup" | "resume" | "compact" |
            # "clear" — which is NOT `resolve_lane`'s source (that one says how
            # the LANE was determined). Conflating the two is easy and would
            # make this log answer a different question than the one asked.
            "source": payload.get("source"),
            # Without this a mid-session `/clear` is indistinguishable from a
            # `/clear` LAUNCH, and two sessions in one worktree are
            # indistinguishable from one — so the log could not answer the
            # per-session question AC1 actually asks. `compaction_marker.py`
            # reads the same field off the same event.
            "session_id": payload.get("session_id"),
            "cwd": payload.get("cwd"),
            "lane": lane,
            "injected": injected,
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
        lines = target.read_text(encoding="utf-8").splitlines(True)
        if len(lines) > FIRE_LOG_MAX:
            target.write_text("".join(lines[-FIRE_LOG_MAX:]), encoding="utf-8")
        return True
    except Exception:
        return False


def handle(payload: dict) -> dict:
    lane, source = resolve_lane(dict(os.environ), payload.get("cwd", os.getcwd()))
    context = build_wakeup(lane, source)
    record_fire(payload, lane, context is not None)
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
