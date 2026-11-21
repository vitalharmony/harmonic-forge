#!/usr/bin/env python3
"""Lane tick: the lane's own task-list entries that this lane owes (harmonic-forge#1477).

The belt's tick used to read only precheck telemetry, so a tick with no belt
event ended in silence even when the lane's own task list held work whose
blocker had cleared or a filing the lane owed. This scan makes that work part
of the tick: one `owed-own-queue <subject>` line per open entry whose
`metadata.owner` is this lane (`lane<LANE>`), nothing for any other owner, and
one `tick-scan: unparsed <subject>` line for an open entry with no or an
unknown owner. Ownership is a structured field the task-list hold hook
enforces at write (`tools/hooks/tasklist_hold.py`), never parsed from the
subject (sticky-wicket REFORGE: no subject grammar survived two preclose
passes). Every failure to read is named on one `tick-scan: …` line; the scan
never exits non-zero, because a tick must not crash.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
import tasklist_owed as owed  # noqa: E402

OPEN = ("pending", "in_progress")
#: The owners `tools/hooks/tasklist_hold.py` enforces as `metadata.owner`.
OWNERS = owed.OWNERS


def classify(entry: dict, lane: str) -> str:
    """`owed`, `other` or `unparsed` for one task-list entry, from its status
    and `metadata.owner` alone (harmonic-forge#1477 sticky-wicket REFORGE):
    the subject's prose is never read, because no subject grammar survived
    two preclose passes. An open issue entry (one whose subject carries an
    issue key, the hold hook's own test) with no or an unknown owner is
    `unparsed`, so the tick names it instead of staying silent. An entry with
    no issue key needs no owner at write (reforge pass 1: the hook and the
    scan must agree on scope), so without one it is never named."""
    if entry.get("status") not in OPEN:
        return "other"
    metadata = entry.get("metadata") or {}
    if not owed.valid_owner(metadata):
        return "unparsed" if owed.entry_key(entry) else "other"
    owner = metadata["owner"]
    return "owed" if owner == f"lane{lane}" else "other"


def scan(store: Path, lane: str) -> list[str]:
    """The tick's output lines for `store`, in entry-id order. A store that
    does not exist yet is an empty list (as `tasklist_owed.py` reads it)."""
    if not store.is_dir():
        return []
    lines: list[str] = []
    for path in sorted(store.glob("*.json"), key=lambda p: (len(p.stem), p.stem)):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(entry, dict) or not isinstance(entry.get("subject"), str) \
                    or not isinstance(entry.get("status"), str) \
                    or not isinstance(entry.get("metadata") or {}, dict):
                raise TypeError("not a task entry")
        except (OSError, ValueError, TypeError):
            lines.append(f"tick-scan: unreadable {path.name}")
            continue
        verdict = classify(entry, lane)
        if verdict == "owed":
            lines.append(f"owed-own-queue {entry['subject']}")
        elif verdict == "unparsed":
            lines.append(f"tick-scan: unparsed {entry['subject']}")
    return lines


def main(argv: list[str] | None = None) -> int:
    session = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    lane = os.environ.get("LANE", "").strip()
    # One resolver with the hold hook (sticky-wicket PATCH): a shared or team
    # list is an accepted limit, named on one line and never scanned.
    store, why = owed.task_store(session)
    if store is None:
        if owed.SHARED_LIST_ENV in why:
            print(f"tick-scan: {why}; not scanned")
        else:
            print("tick-scan: CLAUDE_CODE_SESSION_ID is unset; the task list cannot be read")
        return 0
    if not lane:
        print("tick-scan: LANE is unset; no lane owns this task list")
        return 0
    for line in scan(store, lane):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
