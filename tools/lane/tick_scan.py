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
`waiting on` starts with this lane as a whole token (`Lane 1`, `L1`; never
`L10` or `Lane 12`), or when it has no `waiting on` at all. An own-lane entry
whose next step is already running (`(bg)` or `running`) prints nothing, so a
tick never re-works an in-flight step. Every failure to read is named on one
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
_IN_FLIGHT = re.compile(r"\(bg\)|\brunning\b", re.IGNORECASE)


def owed(subject: str, status: str, lane: str) -> bool:
    """True when this lane is the next actor on an open entry."""
    if status not in OPEN:
        return False
    marker = subject.lower().rfind("waiting on")
    if marker < 0:
        return True
    wait = subject[marker + len("waiting on"):].strip()
    mine = re.match(rf"(lane\s*{re.escape(lane)}|l{re.escape(lane)})(?![0-9a-z])", wait, re.IGNORECASE)
    return bool(mine) and not _IN_FLIGHT.search(wait)


def scan(store: Path, lane: str) -> list[str]:
    """The tick's output lines for `store`, in entry-id order."""
    if not store.is_dir():
        return [f"tick-scan: no task list at {store}"]
    lines: list[str] = []
    for path in sorted(store.glob("*.json"), key=lambda p: (len(p.stem), p.stem)):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            subject, status = str(entry["subject"]), str(entry["status"])
        except (OSError, ValueError, KeyError, TypeError):
            lines.append(f"tick-scan: unreadable {path.name}")
            continue
        if owed(subject, status, lane):
            lines.append(f"owed-own-queue {subject}")
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
