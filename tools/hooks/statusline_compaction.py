#!/usr/bin/env python3
"""`compact:N` for the statusline (harmonic-forge#497).

The operator could not see how many times a session had compacted without
opening its transcript, which is the one number R-0339's restart rule is
stated in terms of. This reads the count from `compaction_marker.py`'s marker
for the current session and prints it.

**Prints nothing at all when there is nothing to say.** A statusline segment
that renders `compact:0` on every session spends permanent screen width on the
default case. Silence until N >= 1 is what makes the segment mean something
when it does appear.

**Never fails the statusline.** Any error — no marker, corrupt marker, missing
module, unreadable directory — prints an empty string and exits 0. A statusline
command that errors either blanks the whole line or spams it; neither is worth
a diagnostic the operator cannot act on anyway.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

#: Compactions after which the restart rule (R-0339) says to restart the lane
#: session rather than carry it further.
RESTART_THRESHOLD = 2


def segment(session_id: str) -> str:
    """`compact:N`, or `compact:N!` at/over the restart threshold, or ``."""
    if not session_id:
        return ""
    try:
        from compaction_marker import read_marker
    except Exception:
        return ""
    try:
        marker = read_marker(session_id) or {}
        count = int(marker.get("compactions", 0))
    except Exception:
        return ""
    if count < 1:
        return ""
    # The `!` is the whole point of the segment: a count is information, a
    # count past the threshold is an instruction (restart at the batch
    # boundary). Rendering both identically would leave the operator to
    # remember the threshold themselves, which is what this replaces.
    return f"compact:{count}" + ("!" if count >= RESTART_THRESHOLD else "")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        session_id = payload.get("session_id") or ""
    except Exception:
        print("", end="")
        return 0
    print(segment(session_id), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
