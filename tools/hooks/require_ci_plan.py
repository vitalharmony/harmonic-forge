#!/usr/bin/env python3
"""`PreToolUse` hook: a lane reads the gate before its first edit or gate run
(harmonic-forge#918).

Operator, 2026-10-06: "fresh lanes blunder into the incremental 'fail and
correct' every time." Until `python3 ~/harmonic-forge/tools/lane/ci_plan.py` has
run successfully in this session, the first `Edit`/`Write`/`MultiEdit`/
`NotebookEdit`, and the first `mise run check|ci-check|check-full`, is denied
with the exact command to run. `ci_plan.py` writes the receipt itself, on its own
success (a PostToolUse payload carries no exit status).

ACTIVE ONLY WHEN: `LANE=2`, or `LANE=1` on a `tooling/*` branch (a Tooling
Exception issue, where Lane 1 is the implementer; no API call, the branch name
is the signal). `LANE=3`, no `LANE`, and a payload with no usable `session_id`
(Codex) are never affected. The operator's typed `ALLOW EDIT` line
(`grant_ci_plan_override.py`) lifts it for the rest of the session.

FAILS OPEN, deliberately: any error, an edit outside every registered project,
a worktree the manifest cannot resolve, allows the tool and prints one line to
stderr. Failing closed would deny every edit in a per-issue worktree with no way
to unlock it (pitch inspection, 2026-10-06). Like every hook here it is a
mistake-detector, not a boundary.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lane"))
from ci_plan import (_git, allow_path, has_receipt, receipt_dir, receipt_path,  # noqa: E402
                     resolve_project)
from shell_parse import command_segments, strip_invocation_prefix  # noqa: E402

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
GATE_TASKS = {"check", "ci-check", "check-full"}


#: `mise` options that take a value, so the value is not read as the task name.
_VALUE_FLAGS = {"-C", "--cd", "-E", "--env", "-j", "--jobs", "-o", "--output"}


def is_gate_command(command: str) -> bool:
    """`mise [run|r] check|ci-check|check-full`, after env assignments, `timeout`,
    `nohup`, `bash -lc` and the like are stripped (the house recognizer)."""
    for segment in command_segments(command):
        tokens = strip_invocation_prefix(segment)
        if not tokens or Path(tokens[0]).name != "mise":
            continue
        rest, index = tokens[1:], 0
        while index < len(rest) and rest[index].startswith("-"):
            index += 2 if rest[index] in _VALUE_FLAGS else 1
        if rest[index:index + 1] in (["run"], ["r"]):
            index += 1
        while index < len(rest) and rest[index].startswith("-"):
            index += 2 if rest[index] in _VALUE_FLAGS else 1
        if rest[index:index + 1] and rest[index] in GATE_TASKS:
            return True
    return False


def target_dir(payload: dict) -> Path:
    """Where the tool acts: the edited file's nearest existing directory, else cwd."""
    path = (payload.get("tool_input") or {}).get("file_path") or (
        payload.get("tool_input") or {}).get("notebook_path")
    if payload.get("tool_name") in EDIT_TOOLS and path:
        directory = Path(path).expanduser().parent
        while not directory.exists() and directory != directory.parent:
            directory = directory.parent
        return directory
    return Path(payload.get("cwd") or os.getcwd())


def _segment_is_gate(segment: list[str]) -> bool:
    # A re-parse that raises is caught in decide(): the walk then adds no target
    # and only the session cwd is judged (preclose pass 2 / post-verdict).
    return is_gate_command(" ".join(segment)) if segment else False


def _mise_dir(segment: list[str]) -> str | None:
    """The literal `-C`/`--cd` value on a `mise` gate segment, else None."""
    tokens = strip_invocation_prefix(segment)
    for index, token in enumerate(tokens[:-1]):
        if token in ("-C", "--cd"):
            return tokens[index + 1]
        if token.startswith("--cd="):
            return token.split("=", 1)[1]
    for token in tokens:
        if token.startswith("--cd="):
            return token.split("=", 1)[1]
    return None


def cd_targets(command: str, cwd: Path) -> list[Path]:
    """harmonic-forge#1343: the directories a leading literal `cd`/`mise -C` moves the
    gate to, walked exactly as `block_unchecked_push._judged_pushes` walks a push
    (`block_unchecked_push.py:45-54`): a subshell or group anywhere, a `$` or
    backtick in a walked segment, or any segment that is neither `cd <literal>` nor
    the gate stops the walk with no target. These are judged in addition to the
    session cwd, never instead of it."""
    if any(char in command for char in "(){}"):
        return []
    here = str(cwd)
    for segment in command_segments(command):
        if any("$" in token or "`" in token for token in segment):
            return []
        if segment[:1] == ["cd"] and len(segment) == 2:
            here = os.path.normpath(os.path.join(here, os.path.expanduser(segment[1])))
            continue
        if _segment_is_gate(segment):
            targets = [Path(here)]
            literal = _mise_dir(segment)
            if literal is not None:
                targets.append(Path(os.path.normpath(os.path.join(here, os.path.expanduser(literal)))))
            return targets
        return []
    return []


def active(lane: str | None, cwd: Path) -> bool:
    if lane == "2":
        return True
    if lane == "1":
        return (_git(cwd, "branch", "--show-current") or "").strip().startswith("tooling/")
    return False


def deny_reason(session_id: str) -> str:
    return ("Read the gate first (harmonic-forge#918): run\n"
            f"  python3 ~/harmonic-forge/tools/lane/ci_plan.py --session {session_id} [path ...]\n"
            "in this checkout (once per checkout and branch). It prints every gate step and the tests that name "
            "the files you change (pass the handoff's Affected Files as paths), and "
            "records that you read it for this session. Then retry this tool call. "
            "The operator can lift this with a typed `ALLOW EDIT` line.")


def _writable(directory: Path) -> bool:
    """Can a receipt be written there, or created beneath its nearest ancestor?"""
    while not directory.exists() and directory != directory.parent:
        directory = directory.parent
    return os.access(directory, os.W_OK)


def decide(payload: dict, lane: str | None) -> str | None:
    """The deny reason, or None to allow. Raises on anything unexpected."""
    tool = payload.get("tool_name")
    if tool in EDIT_TOOLS:
        pass
    elif tool == "Bash":
        command = (payload.get("tool_input") or {}).get("command")
        if not isinstance(command, str) or not is_gate_command(command):
            return None
    else:
        return None
    session_id = payload.get("session_id")
    grant = allow_path(session_id)
    if receipt_path(session_id) is None or grant is None:
        return None
    if grant.is_file():
        return None
    directory = target_dir(payload)
    # harmonic-forge#1343: the session cwd is always judged; a literal `cd`/`mise -C`
    # target of a Bash gate is judged in addition. Deny if any judged directory is
    # active with no receipt.
    judged = [directory]
    if tool == "Bash":
        try:
            extra = cd_targets(payload["tool_input"]["command"], directory)
        except Exception:  # noqa: BLE001 - a walk that cannot parse adds no target; the cwd is still judged
            extra = []
        judged += [d for d in extra if d != directory]
    for index, candidate in enumerate(judged):
        if not active(lane, candidate) or has_receipt(session_id, candidate):
            continue
        if index == 0:
            resolve_project(candidate)  # raises when no registered project owns it: fail open
        else:
            try:
                resolve_project(candidate)
            except Exception:  # noqa: BLE001 - an unregistered cd target adds no deny
                continue
        if not _writable(receipt_dir()):
            raise OSError(f"{receipt_dir()} is not writable, so no receipt or grant can exist")
        return deny_reason(session_id)
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        reason = decide(payload, os.environ.get("LANE")) if isinstance(payload, dict) else None
    except Exception as exc:  # noqa: BLE001 - fail open, one line
        print(f"require_ci_plan: allowed, could not decide ({type(exc).__name__}: {exc})",
              file=sys.stderr)
        return 0
    if reason:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": reason}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
