#!/usr/bin/env python3
"""Lane tick: the lane's own task-list entries that this lane owes (harmonic-forge#1477).

The belt's tick used to read only precheck telemetry, so a tick with no belt
event ended in silence even when the lane's own task list held work whose
blocker had cleared or a filing the lane owed. This scan makes that work part
of the tick: it prints one `owed-own-queue <subject>` line per entry that
names this lane as the next actor (or names nobody), and nothing else.

An entry's subject follows the form the task-list hold hook enforces,
`<KEY> — <last action> · waiting on <who> <step>`. An entry is owed to this
lane when its status is `pending` or `in_progress` and the text after its last
`waiting on` clause names this lane as a whole token (`Lane 1`, `L1`; never
`L10`, `Lane 12` or `Lane 1_x`) or `nothing`, or when it has no clause and is
neither `<KEY> — closed` nor a `YOU:` item. An own-lane entry whose step is
marked `(bg)` prints nothing, so a tick never re-works an in-flight step. A
`<who>` the grammar cannot place prints a `tick-scan: unparsed` line. Every failure to read is named on one
`tick-scan: …` line; the scan never exits non-zero, because a tick must not
crash.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

TASKS_DIR = Path.home() / ".claude" / "tasks"
OPEN = ("pending", "in_progress")
#: Owners `tasklist_hold.py` documents for `<who>` that are not this lane.
_OTHER = re.compile(r"(you|operator|marc|ci|codex)\b", re.IGNORECASE)
_LANE = re.compile(r"(?:lane[\s_:-]*|l)(\d+)(?=$|[\s,.;:)\]])", re.IGNORECASE)
_OWN_WORDS = ("nothing", "nobody")


def _wait_clause(subject: str) -> str | None:
    """The text after `waiting on` in the subject's last ` · `-separated
    segment that starts with it (the shape `tasklist_hold.py` enforces:
    `<KEY> — <last action> · waiting on <who> <step>`), or None. Prose in the
    last-action part never counts as the clause."""
    for segment in reversed(subject.split(" · ")):
        seg = segment.strip()
        if seg.lower().startswith("waiting on"):
            return seg[len("waiting on"):].strip()
    return None


def classify(subject: str, status: str, lane: str) -> str:
    """`owed`, `other` or `unparsed` for one task-list entry.

    Per the hold hook's grammar: `<who>` is this lane (`Lane <N>`, `L<N>`, as a
    whole token), `nothing`/`nobody` (the next step is this lane's own), `YOU:`
    (the operator), CI, or another lane. An entry with no wait clause is owed
    unless it is `<KEY> — closed` or a `YOU:` item. An own-lane entry whose step
    is marked `(bg)` is in flight and not owed. A `<who>` the grammar does not
    place is `unparsed`, so the tick names it instead of staying silent."""
    if status not in OPEN:
        return "other"
    head = subject.split(" · ", 1)[0].strip()
    if subject.lstrip().upper().startswith("YOU:") or re.search(r"—\s*closed\b", head, re.IGNORECASE):
        return "other"
    wait = _wait_clause(subject)
    if wait is None:
        return "owed"
    first = wait.split(None, 1)[0].rstrip(":").lower() if wait.split() else ""
    in_flight = "(bg)" in wait.lower()
    lane_match = _LANE.match(wait)
    if lane_match:
        if lane_match.group(1) != lane:
            return "other"
        return "other" if in_flight else "owed"
    if first in _OWN_WORDS:
        return "other" if in_flight else "owed"
    if in_flight or _OTHER.match(wait):
        return "other"
    return "unparsed"


def owed(subject: str, status: str, lane: str) -> bool:
    """True when this lane is the next actor on an open entry."""
    return classify(subject, status, lane) == "owed"


def scan(store: Path, lane: str) -> list[str]:
    """The tick's output lines for `store`, in entry-id order. A store that
    does not exist yet is an empty list (as `tasklist_owed.py` reads it)."""
    if not store.is_dir():
        return []
    lines: list[str] = []
    for path in sorted(store.glob("*.json"), key=lambda p: (len(p.stem), p.stem)):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            subject, status = entry["subject"], entry["status"]
            if not isinstance(subject, str) or not isinstance(status, str):
                raise TypeError("subject/status not text")
        except (OSError, ValueError, KeyError, TypeError):
            lines.append(f"tick-scan: unreadable {path.name}")
            continue
        verdict = classify(subject, status, lane)
        if verdict == "owed":
            lines.append(f"owed-own-queue {subject}")
        elif verdict == "unparsed":
            lines.append(f"tick-scan: unparsed waiting-on in {subject}")
    return lines


def main(argv: list[str] | None = None) -> int:
    session = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    lane = os.environ.get("LANE", "").strip()
    if not session:
        print("tick-scan: CLAUDE_CODE_SESSION_ID is unset; the task list cannot be read")
        return 0
    if not lane:
        print("tick-scan: LANE is unset; no lane owns this task list")
        return 0
    for line in scan(TASKS_DIR / session, lane):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
