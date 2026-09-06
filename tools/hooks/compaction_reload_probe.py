#!/usr/bin/env python3
"""`PreToolUse` handler recording that a compacted session re-read its corpus
(harmonic-forge#480).

WHAT THIS IS FOR
-------------------
`compaction_marker.py` records that a compaction happened. It cannot record
whether the session then acted on the injection it produced, because
`SessionStart` fires once and never sees a tool call. `PreToolUse` is the only
event that does.

harmonic-forge#451 -- the deny backstop -- needs "has a reload happened since
the boundary", and that question has no answer without this.

WHY THE DETECTION LIVES IN THE OTHER MODULE
----------------------------------------------
`is_corpus_reload` and `CORPUS` are both in `compaction_marker.py`, imported
here. The corpus path list and the "what counts as reading one" list are one
fact in two halves, and that module's own docstring records what happens when
one fact gets a second hand-maintained copy (harmonic-forge#464). #451 imports
the same pair rather than a third copy.

WHY IT IS SAFE TO RUN ON EVERY TOOL CALL
-------------------------------------------
Two early exits before any parsing: no marker file for this session (the
common case -- most sessions never compact), and a marker already carrying
`reloaded_at`. Both are a stat and a small read.

WHAT IT NEVER DOES
---------------------
It never CREATES a marker. A `PreToolUse` that could would manufacture a
compaction that never happened, and #451 would deny on it. It never blocks:
this hook records, and the enforcement half is a separate issue deliberately.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from compaction_marker import MARKER_DIR, is_corpus_reload, note_reload  # noqa: E402


def handle(payload: dict, now: str | None = None) -> dict:
    """Returns the hook's stdout object. `{}` means "do nothing".

    Always `{}` in practice -- this hook has no opinion about the tool call,
    only about the marker beside it. A `PreToolUse` that returned anything
    else would be enforcement, which is harmonic-forge#451's job.
    """
    session_id = payload.get("session_id") or ""
    if not session_id:
        return {}
    if not (MARKER_DIR / f"{session_id}.json").is_file():
        return {}
    if not is_corpus_reload(payload.get("tool_name") or "",
                            payload.get("tool_input") or {}):
        return {}
    note_reload(session_id,
                now or datetime.now(timezone.utc).isoformat())
    return {}


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        # Silent `{}` here, unlike `compaction_marker.main`, and the asymmetry
        # is deliberate: that hook's failure loses a session its recovery
        # note, which is worth a systemMessage. This one's failure loses a
        # flag nothing yet consumes, and a message on every malformed
        # PreToolUse payload would be noise on the hottest event there is.
        print("{}")
        return
    if not isinstance(payload, dict):
        print("{}")
        return
    print(json.dumps(handle(payload)))


if __name__ == "__main__":
    main()
