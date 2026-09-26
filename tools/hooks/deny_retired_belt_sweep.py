#!/usr/bin/env python3
"""Deny any tool call that re-arms the retired account-wide belt sweep
(harmonic-forge#766).

harmonic-forge#640/#659 retired the sweep by operator ruling (it exhausted
the shared REST budget twice on 2026-09-14), but a session invoked it anyway
twelve days later: every surface that describes the retirement -- `--help`,
the old parse-time refusal, `DESIGN.md` -- kept the flag spelled out as a
runnable token, so a "never run X" in context left X available as a concrete
string to reach for. harmonic-forge#766 deletes the flag itself and every
spelled mention; this hook is the backstop for whatever transcript, stale
SKILL.md copy, or memory still names it.

**The message never echoes the token back.** A refusal that quotes the
command it forbids re-teaches it -- the exact mechanism this issue exists to
stop. Say only what the mechanism is and where to find the sanctioned
replacement.
"""
import json
import re
import sys

#: The retired flag's literal spelling -- necessary but not sufficient on its
#: own (see `_SCRIPT_INVOCATION` below); not anchored to the start, so
#: `... && python3 watch_lane_posts.py --sweep-for l1 ...` is caught the same
#: as a bare invocation.
_RETIRED_SWEEP_FLAG = re.compile(r"--sweep-for\b")

#: Preclose fail-direction finding: matching the bare token alone denies the
#: read-only verification AC2 itself requires (a recursive grep for the
#: string, `git log -S`, a transcript read) -- and does so with a message
#: that, by design, never names what tripped, so the false positive reads as
#: broken tooling rather than a diagnosable trigger. Requiring the command to
#: also name the script narrows this to what the hook exists to stop --
#: actually RUNNING the retired mechanism -- while a read-only inspection
#: tool naming both is still exempted below, since that is exactly the AC2
#: verification shape.
_SCRIPT_INVOCATION = re.compile(r"watch_lane_posts\.py\b")

#: A read-only inspection command LEADING ITS OWN SEGMENT is never an
#: execution of the script, even when its own search pattern happens to
#: spell the flag while grepping the script's own source. Preclose
#: fail-direction finding, round 2: matching this anywhere in the whole
#: command (the first version's `.search()`) let a single leading read-only
#: segment exempt an entirely different, later segment that actually runs
#: the script -- `ls && python3 watch_lane_posts.py --sweep-for l1` was not
#: denied. Applied per shell-operator-separated segment instead (below), so
#: only a segment that itself starts with the read-only tool is exempted.
#:
#: Preclose correctness finding, round 3: the original list also included
#: `find` and `awk`, both of which run arbitrary programs in their own right
#: (`find ... -exec ...`, `awk 'BEGIN{system(...)}'`) -- a single segment
#: leading with either exempted a real execution of the retired mechanism.
#: `sed` has the same property via its GNU `e` command and is left off for
#: the same reason. Only genuinely inert inspection commands remain.
_READ_ONLY_LEADING = re.compile(
    r"^\s*(?:sudo\s+)?(?:grep|rg|ag|git\s+(?:log|grep|show|diff)|"
    r"cat|head|tail|wc|ls)\b"
)

#: Shell command/pipeline separators. Not a full shell parser -- this is a
#: deny-list backstop, not a sandbox -- but splitting on the operators that
#: actually chain independent commands is what makes the per-segment
#: exemption above sound rather than a whole-string escape hatch.
#:
#: Preclose finding, round 4: a bare newline is also a command separator in
#: bash (and in a `$'...'`-quoted or heredoc argument), and was missing from
#: this class -- `grep foo\npython3 watch_lane_posts.py --sweep-for l1` was
#: read as a SINGLE segment starting with `grep`, so the read-only exemption
#: covered the second line's actual execution of the retired mechanism.
_SEGMENT_SPLIT = re.compile(r"[;&|\n]+")

_MESSAGE = (
    "The account-wide belt sweep is retired (harmonic-forge#640/#659). Arm "
    "only what `python3 ~/harmonic-forge/tools/lane/belt_plan.py` prints."
)

_DENY = {
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": _MESSAGE,
    },
    "systemMessage": _MESSAGE,
}


def decision(command: str) -> dict:
    text = command or ""
    for segment in _SEGMENT_SPLIT.split(text):
        seg = segment.strip().lstrip("(")
        if not _RETIRED_SWEEP_FLAG.search(seg):
            continue
        if not _SCRIPT_INVOCATION.search(seg):
            continue
        if _READ_ONLY_LEADING.match(seg):
            continue
        return _DENY
    return {}


#: Preclose silent-bypass finding: the belt's actual arming channel is the
#: `Monitor` tool, not `Bash` -- a Bash-only hook leaves the exact incident
#: this issue exists to stop (#640/#659) fully reachable via a Monitor command,
#: and worse, on a repeating tick rather than once. Matches the field each
#: tool carries a shell-like string in, mirroring `enforce_belt_arming.py`'s
#: own coverage of the sibling incident-replay guard: `Bash`/`Monitor` ->
#: `command`, `CronCreate` -> `prompt`, `Skill` -> `args` (a `/loop` prompt
#: can be paraphrased into a frozen Cron job or Skill invocation, same as the
#: original incident).
_FIELD_BY_TOOL = {
    "Bash": "command",
    "Monitor": "command",
    "CronCreate": "prompt",
    "Skill": "args",
}


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        print("{}")
        return
    field = _FIELD_BY_TOOL.get(payload.get("tool_name") or "")
    if field is None:
        print("{}")
        return
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        print("{}")
        return
    print(json.dumps(decision(str(tool_input.get(field) or ""))))


if __name__ == "__main__":
    main()
