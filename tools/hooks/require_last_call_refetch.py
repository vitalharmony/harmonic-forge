"""Stop-hook reminder: re-fetch in-flight threads before ending a turn
(harmonic-forge#496, R-0347).

Promoted from `feedback_never_stop_a_batch_without_an_open_question` and
`feedback_poll_monitor_issues_between_actions` — two memories, two recorded
instances each in single sessions, one rule.

**This ADVISES, it does not block.** A Stop hook cannot see whether the session
already re-fetched, so a deny here would fire on correct turns as often as
incorrect ones and would be turned off within a day. What it can do is make the
omission visible at exactly the moment it happens, which is the whole of the
failure: a batch stops because a thread *looked* blocked, and nobody re-read it.

Silence is the failure mode, so this always emits when it has something to say
and never when it does not.
"""
import json
import sys

_REMINDER = (
    "Before ending this turn (R-0347): re-fetch the newest comment on every "
    "in-flight thread. Only an open HITL question stops a batch — and "
    "\"blocked on another lane\" must cite the comment id that establishes it, "
    "not an inference from a thread you read earlier."
)


def build(payload: dict) -> dict:
    """Emit the reminder unless the session is already stopping on a hook."""
    if payload.get("stop_hook_active"):
        # Already inside a stop-hook cycle; re-emitting would loop.
        return {}
    return {"systemMessage": _REMINDER}


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}
    out = build(payload)
    print(json.dumps(out) if out else "{}")


if __name__ == "__main__":
    main()
