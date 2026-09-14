#!/usr/bin/env python3
"""`PreToolUse` guard: belt-and-suspenders is armed with exactly the calls
`tools/lane/belt_plan.py` prints, or not at all (harmonic-forge#659).

Matcher: `Monitor|CronCreate|Skill`. Active only when `LANE` is 1, 2 or 3.

WHY A HOOK
----------
On 2026-09-14 Lane 3 armed the protocol wrong after the canonical table (#651)
already existed: a multi-paragraph `/loop` prompt it wrote itself, frozen into a
`CronCreate` job, and the repo-wide `--sweep-for l3` sweep that exhausted the
shared REST budget twice that day. The literal strings were in context. Prose
was not enough; `watch_lane_posts.py` validates its own argv but nothing saw the
tool calls. This does.

WHAT IS DENIED
--------------
- `Monitor` whose command runs `watch_lane_posts.py` and is not, after
  whitespace normalization, the lane's canonical command (`~`, `$HOME`,
  `${HOME}` and the absolute home path are equivalent spellings).
- `Skill` with `skill` `loop` (or `<plugin>:loop`) whose args mention finding
  work, the belt or the suspenders but are not exactly
  `10m proactively find work to do`.
- `CronCreate` whose prompt mentions finding work, the belt, the suspenders or
  `watch_lane_posts`, unless the prompt is exactly
  `proactively find work to do`. That one exception is not a second arming
  path: it is the call the `/loop` skill itself makes for
  `/loop 10m proactively find work to do` (observed in real transcripts as
  `{"cron": "*/10 * * * *", "prompt": "proactively find work to do",
  "recurring": true}`), so denying it would deny the canonical arm.

Unrelated Monitors, crons and skills pass untouched.

HOW IT DENIES
-------------
JSON on stdout (`hookSpecificOutput.permissionDecision: "deny"`), exit 0 --
never exit 2, so the guarded registration's `|| true` swallows nothing. Every
deny reason quotes the exact calls from `belt_plan.canonical_calls`. The hook
fails open on its own exceptions: a broken guard must never wedge a lane.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

LANES = ("1", "2", "3")

#: Mentions that make a cron prompt or loop args an arming attempt.
ARMING_RE = re.compile(
    r"find(?:s|ing)?\s+(?:\w+\s+)?work|belt|suspenders|watch_lane_posts",
    re.IGNORECASE,
)

#: The prompt the `/loop` skill hands `CronCreate` for the canonical loop.
LOOP_CRON_PROMPT = "proactively find work to do"


def _load_belt_plan():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lane"))
    import belt_plan  # noqa: PLC0415
    return belt_plan


def _normalize_command(command: str) -> str:
    text = " ".join(command.split())
    home = os.path.expanduser("~").rstrip("/")
    for spelling in ("${HOME}/", "$HOME/", home + "/"):
        text = text.replace(spelling, "~/")
    return text


def _reason(lane: str, calls: dict[str, Any], what: str) -> str:
    return (
        f"{what}\n\n"
        f"LANE={lane} arms belt-and-suspenders with exactly these two calls, "
        "character for character (from `python3 ~/harmonic-forge/tools/lane/"
        "belt_plan.py`, harmonic-forge#659):\n"
        f"  Monitor {json.dumps(calls['monitor'])}\n"
        f"  Skill {json.dumps(calls['loop'])}\n"
        "Arming is Skill(loop) plus that Monitor only -- never CronCreate "
        "directly, never a hand-written prompt, never a repo-wide sweep."
    )


def decide(payload: dict[str, Any], lane: str | None) -> str | None:
    """The deny reason for this tool call, or None to allow."""
    if lane not in LANES:
        return None
    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return None

    if tool == "Monitor":
        command = tool_input.get("command") or ""
        if not isinstance(command, str) or "watch_lane_posts.py" not in command:
            return None
        belt_plan = _load_belt_plan()
        calls = belt_plan.canonical_calls(lane)
        if _normalize_command(command) == _normalize_command(calls["monitor"]["command"]):
            return None
        return _reason(lane, calls,
                       "Denied: this Monitor runs watch_lane_posts.py but is not "
                       f"LANE={lane}'s canonical belt command.")

    if tool == "Skill":
        skill = tool_input.get("skill") or ""
        if not isinstance(skill, str) or not (skill == "loop" or skill.endswith(":loop")):
            return None
        args = tool_input.get("args") or ""
        if not isinstance(args, str) or not ARMING_RE.search(args):
            return None
        belt_plan = _load_belt_plan()
        calls = belt_plan.canonical_calls(lane)
        if args.strip() == calls["loop"]["args"]:
            return None
        return _reason(lane, calls,
                       "Denied: this /loop prompt is a hand-written or paraphrased "
                       "suspenders prompt, not the canonical one.")

    if tool == "CronCreate":
        prompt = tool_input.get("prompt") or ""
        if not isinstance(prompt, str) or not ARMING_RE.search(prompt):
            return None
        if prompt.strip() == LOOP_CRON_PROMPT:
            return None
        belt_plan = _load_belt_plan()
        calls = belt_plan.canonical_calls(lane)
        return _reason(lane, calls,
                       "Denied: CronCreate is not a way to arm the suspenders.")

    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            print(json.dumps({}))
            return 0
        reason = decide(payload, os.environ.get("LANE"))
    except Exception as exc:  # noqa: BLE001 - fail open, visibly
        print(json.dumps({"systemMessage":
                          f"enforce_belt_arming: guard did not run ({type(exc).__name__}: {exc})"}))
        return 0
    if reason is None:
        print(json.dumps({}))
        return 0
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
