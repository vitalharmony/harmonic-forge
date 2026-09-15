#!/usr/bin/env python3
"""`PreToolUse` guard: belt-and-suspenders is armed with exactly the calls
`tools/lane/belt_plan.py` prints, or not at all (harmonic-forge#659).

Matcher: `Monitor|CronCreate|Skill|Bash|Write|Edit|MultiEdit|NotebookEdit`.
The arming checks are active only when `LANE` is 1, 2 or 3; the grant-directory
write guard (below) is active in every session.

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

OPERATOR OVERRIDE: `ALLOW LOOP`
------------------------------
A line the operator types starting `ALLOW LOOP` makes `grant_loop_override.py`
(`UserPromptSubmit`) write `<session_id>.loop_grant` into the arming directory.
The grant is valid for `GRANT_TTL_SECONDS` and covers ONE non-canonical arming:

- a non-canonical `Skill` `loop` is allowed and records its args in the grant,
  WITHOUT consuming it, because the loop skill's own `CronCreate` follows;
- a non-canonical `CronCreate` consumes (deletes) the grant. After a `Skill`
  call it must be that skill's cron -- its prompt a substring of the recorded
  args -- so the pair is one arming, and a second `Skill` under the same grant
  is denied. With no prior `Skill` call, any one `CronCreate` consumes it.

The override never touches the `Monitor`/`watch_lane_posts.py` check, and an
expired grant is deleted and ignored.

Agents cannot write the grant: any `Bash` or `Monitor` command that mentions
the arming directory (or a `.loop_grant` file) is denied unless every command
segment is a read-only program with no redirect, and `Write`/`Edit`/
`MultiEdit`/`NotebookEdit` into the directory are denied. Accident prevention
against a self-minted override, not a sandbox (same framing as
`batch_provenance.py`).

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
import time
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

#: `ALLOW LOOP` grants: `<session_id>` + this suffix, valid for the TTL.
GRANT_SUFFIX = ".loop_grant"
GRANT_TTL_SECONDS = 600

#: Programs that cannot write a file on their own (a redirect is checked
#: separately). Anything else mentioning the arming directory is denied.
_READ_ONLY_PROGRAMS = frozenset({
    "ls", "cat", "stat", "head", "tail", "grep", "rg", "wc", "file", "cd",
    "pwd", "test", "[", "echo", "printf", "true", "find", "du", "jq", "less",
})
_FIND_WRITE_FLAGS = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir",
                               "-fprint", "-fprint0", "-fprintf", "-fls"})
_HARMLESS_REDIRECT_RE = re.compile(r"^\d*>>?(?:/dev/null|&\d)$")
_WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_PYTHON_RE = re.compile(r"^python(\d+(\.\d+)?)?$")


def _load_belt_plan():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lane"))
    import belt_plan  # noqa: PLC0415
    return belt_plan


def arming_dir() -> Path:
    override = os.environ.get(ARMING_DIR_ENV)
    return Path(override) if override else DEFAULT_ARMING_DIR


def grant_path(session_id: object) -> Path | None:
    """Where `session_id`'s `ALLOW LOOP` grant lives, or None for a bad id."""
    if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
        return None
    return arming_dir() / (session_id + GRANT_SUFFIX)


def write_grant(session_id: str, reason: str, now: float | None = None) -> Path | None:
    """Record a fresh grant (replacing any earlier one). Hook-internal only."""
    path = grant_path(session_id)
    if path is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    created = time.time() if now is None else now
    path.write_text(json.dumps({"created": created, "reason": reason}) + "\n",
                    encoding="utf-8")
    return path


def read_grant(session_id: object, now: float | None = None) -> dict[str, Any] | None:
    """The live grant for this session, or None. An expired or unreadable
    grant is deleted: it must not linger to be revived."""
    path = grant_path(session_id)
    if path is None or not path.exists():
        return None
    now = time.time() if now is None else now
    try:
        grant = json.loads(path.read_text(encoding="utf-8"))
        created = float(grant["created"])
    except (OSError, ValueError, KeyError, TypeError):
        path.unlink(missing_ok=True)
        return None
    if not (created <= now + 60 and now - created <= GRANT_TTL_SECONDS):
        path.unlink(missing_ok=True)
        return None
    return grant


def _grant_markers() -> list[str]:
    markers = {str(arming_dir()), str(DEFAULT_ARMING_DIR),
               "harmonic-forge/belt_arming", "belt_arming/", GRANT_SUFFIX}
    return sorted(markers)


def _mentions_arming_dir(text: str) -> bool:
    home = os.path.expanduser("~").rstrip("/")
    expanded = (text.replace("${HOME}", home).replace("$HOME", home)
                .replace("~/", home + "/"))
    return any(marker in expanded for marker in _grant_markers())


def _segment_is_read_only(tokens: list[str]) -> bool:
    for index, token in enumerate(tokens):
        if ">" in token and not _HARMLESS_REDIRECT_RE.match(token):
            following = tokens[index + 1] if index + 1 < len(tokens) else ""
            if not (re.match(r"^\d*>>?$", token) and following in ("/dev/null", "&1", "&2")):
                return False
    tokens = strip_invocation_prefix(tokens)
    if not tokens:
        return True
    program = Path(tokens[0]).name
    if program not in _READ_ONLY_PROGRAMS:
        return False
    return not (program == "find" and _FIND_WRITE_FLAGS & set(tokens[1:]))


def command_writes_arming_dir(command: str) -> bool:
    """True when a shell command mentions the arming directory and is not
    plainly read-only -- the test for a self-written `ALLOW LOOP` grant."""
    if not _mentions_arming_dir(command):
        return False
    try:
        segments = command_segments(command)
    except ValueError:  # unbalanced quotes: cannot show it is read-only
        return True
    return not all(_segment_is_read_only(segment) for segment in segments)


def _path_in_arming_dir(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    return _mentions_arming_dir(value)


def _write_guard_reason(tool: str, tool_input: dict[str, Any]) -> str | None:
    denial = ("Denied: this {what} writes into the belt-arming state directory "
              f"({arming_dir()}). That directory holds hook-owned state -- the "
              "per-session arming record and the operator's ALLOW LOOP grant -- "
              "and only the hooks write it (harmonic-forge#659). Reading it is "
              "fine; nothing in a lane session needs to write it.")
    if tool in ("Bash", "Monitor"):
        command = tool_input.get("command")
        if isinstance(command, str) and command_writes_arming_dir(command):
            return denial.format(what=f"{tool} command")
    elif tool in _WRITE_TOOLS:
        for key in ("file_path", "notebook_path", "path"):
            if _path_in_arming_dir(tool_input.get(key)):
                return denial.format(what=f"{tool} call")
    return None


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


_OVERRIDE_HINT = (
    " If the operator wants a one-off non-canonical /loop or CronCreate in this "
    "session, the operator can grant one with a line starting `ALLOW LOOP` in "
    "their own message (valid 10 minutes, one use).")


def _reason(lane: str, calls: dict[str, Any], what: str, override_hint: bool = False) -> str:
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
        + (_OVERRIDE_HINT if override_hint else "")
    )


def _is_canonical_cron(tool_input: dict[str, Any]) -> bool:
    return (tool_input.get("cron") == LOOP_CRON["cron"]
            and tool_input.get("prompt") == LOOP_CRON["prompt"]
            and tool_input.get("recurring", True) is True)


def decide(payload: dict[str, Any], lane: str | None,
           notes: list[str] | None = None) -> str | None:
    """The deny reason for this tool call, or None to allow.

    Allowing the canonical CronCreate records it for the session, so the same
    session's next canonical CronCreate is denied as already armed. Allowing a
    call under an `ALLOW LOOP` grant appends a line to `notes` for the caller
    to surface.
    """
    notes = [] if notes is None else notes
    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return None
    write_denial = _write_guard_reason(tool, tool_input)
    if write_denial is not None:
        return write_denial
    if lane not in LANES:
        return None
    session_id = payload.get("session_id")

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
        grant = read_grant(session_id)
        if grant is not None and "skill_args" not in grant:
            grant["skill_args"] = args.strip() if isinstance(args, str) else ""
            path = grant_path(session_id)
            assert path is not None  # read_grant returned a grant for this id
            path.write_text(json.dumps(grant) + "\n", encoding="utf-8")
            notes.append("enforce_belt_arming: non-canonical /loop allowed under the "
                         "operator's ALLOW LOOP grant; its CronCreate consumes it.")
            return None
        what = "Denied: in a lane session /loop runs only the canonical suspenders prompt."
        if grant is not None:
            what += (" The operator's ALLOW LOOP grant already covered one /loop "
                     "in this session.")
        return _reason(lane, calls, what, override_hint=True)

    if tool == "CronCreate":
        belt_plan = _load_belt_plan()
        calls = belt_plan.canonical_calls(lane)
        if not _is_canonical_cron(tool_input):
            grant = read_grant(session_id)
            if grant is not None:
                skill_args = grant.get("skill_args")
                prompt = tool_input.get("prompt")
                if skill_args is None or (isinstance(prompt, str) and prompt.strip()
                                          and prompt.strip() in skill_args):
                    path = grant_path(session_id)
                    assert path is not None
                    path.unlink(missing_ok=True)
                    notes.append("enforce_belt_arming: non-canonical CronCreate allowed; "
                                 "the operator's ALLOW LOOP grant is now consumed.")
                    return None
            return _reason(lane, calls,
                           "Denied: in a lane session CronCreate is allowed only as "
                           "the canonical suspenders job the loop skill creates.",
                           override_hint=True)
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
        notes: list[str] = []
        reason = decide(payload, os.environ.get("LANE"), notes)
    except Exception as exc:  # noqa: BLE001 - fail open, visibly
        print(json.dumps({"systemMessage":
                          f"enforce_belt_arming: guard did not run ({type(exc).__name__}: {exc})"}))
        return 0
    if reason is None:
        print(json.dumps({"systemMessage": " ".join(notes)} if notes else {}))
        return 0
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
