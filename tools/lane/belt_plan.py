#!/usr/bin/env python3
"""The one source of the exact tool calls that arm belt-and-suspenders
(harmonic-forge#659).

On 2026-09-14 a lane armed the protocol wrong for the second time after the
canonical table existed (#651): it paraphrased the `/loop` prompt, froze it into
a `CronCreate` job, and armed the repo-wide `--sweep-for l3` the skill's own
prose told it to. Every one of those was a lane re-deriving a call from prose.
So the calls are no longer prose. This script prints them for `$LANE`, the
skill says "make exactly these calls", and `tools/hooks/enforce_belt_arming.py`
denies any other arming call, quoting this script's output.

Usage:
    python3 ~/harmonic-forge/tools/lane/belt_plan.py

Prints a JSON object:
    monitor  -- the `Monitor` tool input: command, description, timeout_ms.
                The command is built from `watch_lane_posts.CANONICAL_BELTS`,
                never retyped here.
    loop     -- the `Skill` tool input: {"skill": "loop", "args": ...}, i.e.
                the literal `/loop 10m proactively find work to do`.
    report   -- what to report once both are armed.

Exits 2 when `LANE` is not 1, 2 or 3: no lane means no protocols.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gh"))
from watch_lane_posts import CANONICAL_BELTS, MONITOR_LIFETIME_S  # noqa: E402

LANES = ("1", "2", "3")

#: The script path exactly as the Monitor command spells it. `~` is expanded by
#: the shell the Monitor runs in; the hook accepts `$HOME` and the absolute
#: path as equivalent spellings.
WATCHER = "~/harmonic-forge/tools/gh/watch_lane_posts.py"

#: Monitor's own maximum (`timeout_ms` above 1800000 is capped to it).
#:
#: harmonic-forge#680: DERIVED, not declared. The poller holds the same number
#: as `--deadline-seconds` and refuses to schedule a sleep past it; two
#: independently-maintained numbers is exactly what produced that defect (a
#: 3000s backoff cap inside an 1800s container, with nothing checking them
#: together).
MONITOR_TIMEOUT_MS = MONITOR_LIFETIME_S * 1000

LOOP_SKILL = "loop"
LOOP_ARGS = "10m proactively find work to do"


def monitor_command(lane: str) -> str:
    """The canonical Monitor command for `lane`, from `CANONICAL_BELTS`."""
    return "python3 " + WATCHER + " " + " ".join(CANONICAL_BELTS[lane][0]["argv"])


def canonical_calls(lane: str) -> dict[str, Any]:
    """The exact arming calls for `lane`. Raises KeyError for an invalid lane."""
    if lane not in LANES or lane not in CANONICAL_BELTS:
        raise KeyError(lane)
    return {
        "monitor": {
            "command": monitor_command(lane),
            "description": f"Lane {lane} belt",
            "timeout_ms": MONITOR_TIMEOUT_MS,
        },
        "loop": {"skill": LOOP_SKILL, "args": LOOP_ARGS},
        "report": (
            f"LANE={lane} armed: monitor task id <id from Monitor>, loop job id "
            "<id from /loop>. Then stop. When the Monitor expires, re-arm the "
            "same Monitor call unchanged. Never ask the operator whether to stop."
        ),
    }


def main() -> int:
    lane = os.environ.get("LANE", "")
    try:
        calls = canonical_calls(lane)
    except KeyError:
        print(
            f"belt_plan: LANE={lane!r} is not 1, 2 or 3 -- no lane means no "
            "protocols, neither one. Launch via lane1/lane2/lane3; do not set "
            "LANE inline and do not fall back to Lane 1 (harmonic-forge#659).",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(calls, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
