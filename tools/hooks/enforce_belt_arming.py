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

WHAT IS DENIED (in a LANE=1/2/3 session)
----------------------------------------
- `Skill` `loop` (or `<plugin>:loop`) whose args, stripped, are not exactly
  `10m proactively find work to do`.
- `CronCreate` that is not exactly `{"cron": "*/10 * * * *", "prompt":
  "proactively find work to do", "recurring": true}` (`recurring` absent counts
  as true, the tool's default). That is the call the `/loop` skill makes for the
  canonical loop, observed in real transcripts right after the `Skill` call.
- A second canonical `CronCreate` in the same session ("already armed"): the
  first allowed one is recorded under `~/.cache/harmonic-forge/belt_arming/
  <session_id>`, so a re-arm cannot stack a duplicate job (#659 preclose C).
- `Monitor` whose command *executes* `watch_lane_posts.py` -- as the program, or
  as the script argument to `python`/`python3` -- and is not, after whitespace
  normalization, the lane's canonical command (`~`, `$HOME`, `${HOME}` and the
  absolute home path are equivalent spellings). Commands that only mention the
  name (`pgrep -af watch_lane_posts.py`, `grep`, `tail`) are allowed.

There is no keyword matching (#659 preclose A and D): a regex over free text
let reworded suspenders prompts through and denied unrelated crons. Instead,
every non-canonical loop or cron in a lane session is denied, and the deny
reason points other timed work at `Monitor` or Bash `run_in_background`, which
this hook does not restrict. Back-off between ticks is `ScheduleWakeup`, which
this hook does not govern either.

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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shell_parse import command_segments, strip_invocation_prefix  # noqa: E402

LANES = ("1", "2", "3")

#: The `CronCreate` the `/loop` skill makes for `/loop 10m proactively find work
#: to do` (observed in real transcripts). `recurring` absent means true.
LOOP_CRON = {"cron": "*/10 * * * *", "prompt": "proactively find work to do",
             "recurring": True}

WATCHER_NAME = "watch_lane_posts.py"

#: Where the per-session "canonical cron already allowed" markers live.
#: Overridable for tests.
ARMING_DIR_ENV = "HARMONIC_FORGE_BELT_ARMING_DIR"
DEFAULT_ARMING_DIR = Path.home() / ".cache" / "harmonic-forge" / "belt_arming"

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_PYTHON_RE = re.compile(r"^python(\d+(\.\d+)?)?$")


def _load_belt_plan():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lane"))
    import belt_plan  # noqa: PLC0415
    return belt_plan


def arming_dir() -> Path:
    override = os.environ.get(ARMING_DIR_ENV)
    return Path(override) if override else DEFAULT_ARMING_DIR


def _normalize_command(command: str) -> str:
    text = " ".join(command.split())
    home = os.path.expanduser("~").rstrip("/")
    for spelling in ("${HOME}/", "$HOME/", home + "/"):
        text = text.replace(spelling, "~/")
    return text


def _executes_watcher(tokens: list[str]) -> bool:
    """True when this command segment runs `watch_lane_posts.py`."""
    tokens = strip_invocation_prefix(tokens)
    if tokens and tokens[0] == "timeout":
        rest = tokens[1:]
        while rest and rest[0].startswith("-"):
            rest = rest[1:]
        return _executes_watcher(rest[1:])  # drop the DURATION
    if not tokens:
        return False
    program = Path(tokens[0]).name
    if program == WATCHER_NAME:
        return True
    if _PYTHON_RE.match(program):
        for arg in tokens[1:]:
            if not arg.startswith("-"):
                return Path(arg).name == WATCHER_NAME
    return False


def _monitor_runs_watcher(command: str) -> bool:
    try:
        segments = command_segments(command)
    except ValueError:  # unbalanced quotes: the shell would refuse it too
        return WATCHER_NAME in command
    return any(_executes_watcher(segment) for segment in segments)


def _reason(lane: str, calls: dict[str, Any], what: str) -> str:
    return (
        f"{what}\n\n"
        f"LANE={lane} arms belt-and-suspenders with exactly these two calls, "
        "character for character (from `python3 ~/harmonic-forge/tools/lane/"
        "belt_plan.py`, harmonic-forge#659):\n"
        f"  Monitor {json.dumps(calls['monitor'])}\n"
        f"  Skill {json.dumps(calls['loop'])}\n"
        "The loop skill then makes exactly this CronCreate, once per session:\n"
        f"  CronCreate {json.dumps(LOOP_CRON)}\n"
        "In a lane session those are the only /loop and CronCreate calls allowed. "
        "For any other timed or repeated work use Monitor (a streaming command) "
        "or Bash with run_in_background -- neither is restricted. Back off "
        "between suspenders ticks with ScheduleWakeup, never a new /loop or "
        "CronCreate."
    )


def _is_canonical_cron(tool_input: dict[str, Any]) -> bool:
    return (tool_input.get("cron") == LOOP_CRON["cron"]
            and tool_input.get("prompt") == LOOP_CRON["prompt"]
            and tool_input.get("recurring", True) is True)


def decide(payload: dict[str, Any], lane: str | None) -> str | None:
    """The deny reason for this tool call, or None to allow.

    Allowing the canonical CronCreate records it for the session, so the same
    session's next canonical CronCreate is denied as already armed.
    """
    if lane not in LANES:
        return None
    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return None

    if tool == "Monitor":
        command = tool_input.get("command") or ""
        if not isinstance(command, str) or not _monitor_runs_watcher(command):
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
        belt_plan = _load_belt_plan()
        calls = belt_plan.canonical_calls(lane)
        args = tool_input.get("args")
        if isinstance(args, str) and args.strip() == calls["loop"]["args"]:
            return None
        return _reason(lane, calls,
                       "Denied: in a lane session /loop runs only the canonical "
                       "suspenders prompt.")

    if tool == "CronCreate":
        belt_plan = _load_belt_plan()
        calls = belt_plan.canonical_calls(lane)
        if not _is_canonical_cron(tool_input):
            return _reason(lane, calls,
                           "Denied: in a lane session CronCreate is allowed only as "
                           "the canonical suspenders job the loop skill creates.")
        session_id = payload.get("session_id")
        if isinstance(session_id, str) and _SESSION_ID_RE.match(session_id):
            marker = arming_dir() / session_id
            if marker.exists():
                return _reason(lane, calls,
                               "Denied: the suspenders are already armed in this "
                               f"session (recorded at {marker}); a second job would "
                               "stack duplicate ticks. Nothing to re-arm -- the "
                               "existing job keeps running.")
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps(LOOP_CRON) + "\n", encoding="utf-8")
        return None

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
