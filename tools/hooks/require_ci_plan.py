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
from ci_plan import _git, allow_path, receipt_path, resolve_project  # noqa: E402
from shell_parse import command_segments  # noqa: E402

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
GATE_TASKS = {"check", "ci-check", "check-full"}


def is_gate_command(command: str) -> bool:
    for tokens in command_segments(command):
        if tokens and Path(tokens[0]).name == "mise":
            rest = [t for t in tokens[1:] if not t.startswith("-")]
            if rest[:1] in (["run"], ["r"]):
                rest = rest[1:]
            if rest[:1] and rest[0] in GATE_TASKS:
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


def active(lane: str | None, cwd: Path) -> bool:
    if lane == "2":
        return True
    if lane == "1":
        return (_git(cwd, "branch", "--show-current") or "").strip().startswith("tooling/")
    return False


def deny_reason() -> str:
    return ("Read the gate first (harmonic-forge#918): run\n"
            "  python3 ~/harmonic-forge/tools/lane/ci_plan.py [path ...]\n"
            "in this checkout, once. It prints every gate step and the tests that name "
            "the files you change (pass the handoff's Affected Files as paths), and "
            "records that you read it for this session. Then retry this tool call. "
            "The operator can lift this with a typed `ALLOW EDIT` line.")


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
    receipt, grant = receipt_path(session_id), allow_path(session_id)
    if receipt is None or grant is None:
        return None
    directory = target_dir(payload)
    if not active(lane, directory):
        return None
    if receipt.is_file() or grant.is_file():
        return None
    resolve_project(directory)  # raises when no registered project owns it: fail open
    return deny_reason()


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
