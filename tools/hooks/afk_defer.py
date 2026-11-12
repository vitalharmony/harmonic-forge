#!/usr/bin/env python3
"""Defer prompting actions while afk is on (harmonic-forge#1338): PreToolUse, Bash.

When `~/.claude/state/afk/state.json` is absent this hook is inert: exit 0,
no output. When it is present, it calls `batch_auth.decide(command)`, the
existing authority on which `gh` commands Ask (issue close in any form, a
GraphQL mutation, an unauthorized `gh pr merge`). On `("ask", reason)` it
appends one line to `~/.claude/state/afk/queue.jsonl` under the afk lock and
denies with the queue message, so the turn continues instead of blocking on a
prompt nobody is there to answer. On `allow` or `None` it outputs nothing and
`batch_auth`'s own result stands.

The deny list (AC7) is closed and lives in one place: exactly the `ask`
returns of `batch_auth.decide()`. This hook never parses `gh` text itself, and
it only adds denies; it never grants or relaxes an authorization.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import afk_toggle  # noqa: E402

DENY_MESSAGE = ("afk: queued for the operator's return (batch_auth would ask); "
                "continue with other work.")


def defer(payload: dict, now: float | None = None) -> str | None:
    """The deny reason, or None to stay silent."""
    if not afk_toggle.is_on():
        return None
    if payload.get("tool_name") not in ("Bash", None):
        return None
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command.strip():
        return None
    import batch_auth  # noqa: PLC0415
    result = batch_auth.decide(command)
    if not result or result[0] != "ask":
        return None
    # Only an ask on a segment batch_auth classifies is deferred; a
    # parse-failure ask stays with batch_auth (its prompt, not a deny).
    try:
        segments = batch_auth.command_segments(command)
    except ValueError:
        return None
    if not any(batch_auth._protected_graphql(t) or batch_auth.classify_issue_close(t) is not None
               or batch_auth.classify_pr_merge(t) is not None for t in segments):
        return None
    now = time.time() if now is None else now
    cwd = payload.get("cwd") or os.getcwd()
    queued = afk_toggle.append_queue({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "session": payload.get("session_id"),
        "lane": os.environ.get("LANE"),
        "cwd": cwd,
        "workspace": afk_toggle.workspace_of(cwd),
        "command": command,
        "reason_head": (str(result[1]).strip().splitlines() or [""])[0][:200],
    })
    # `off` landed between the outer check and the lock: allow (silence).
    return DENY_MESSAGE if queued else None


def main() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        reason = defer(payload)
    except Exception:  # noqa: BLE001 - never deny on our own failure; batch_auth still asks
        return 0
    if reason:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
