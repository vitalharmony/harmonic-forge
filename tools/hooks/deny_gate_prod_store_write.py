#!/usr/bin/env python3
"""Deny any agent tool call that touches the gate-prod consumed-receipt store
(harmonic-forge#878, R-0377).

HRSE2's `scripts/gate_production_run.py` writes one receipt per production run
it executes, keyed (repo, issue, SHA, action, AE comment id), to
`~/.claude/state/gate-prod/` -- outside every tracked tree -- and refuses a
second run on a consumed key: one AE is one run. A receipt an agent can delete
or rewrite is no receipt, so this hook denies, in every lane and session:

  * a Bash command naming that store (`gate-prod` alongside `.claude` or
    `state`, in any quoting), and
  * an Edit / Write / MultiEdit / NotebookEdit whose path resolves into it.

The script itself writes the store from its own process, never through a
tool call, so nothing legitimate is denied. Reading a receipt by hand is
denied too: telling a read from a write needs a shell parser this hook is
deliberately not, and the receipts hold nothing a session needs.

Agent accident and the casual workaround are the threat model (the same as
`block_irreversible_ops.py`); `python3 -c` with an assembled path string
passes every hook. The receipt is checked again by the script, under its own
exclusive create, so a deleted receipt can never turn into a second run
without a tool call naming the store first.
"""
from __future__ import annotations

import json
import os
import pwd
import re
import sys
from pathlib import Path

#: The real account home, never `$HOME` -- an inherited `HOME=` must not move it.
STORE = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".claude" / "state" / "gate-prod"

_NAMES_STORE = re.compile(r"gate[\W_]*prod", re.I)
_NAMES_STATE = re.compile(r"\.claude|\bstate\b", re.I)
_PATH_TOOLS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path",
               "NotebookEdit": "notebook_path"}

REASON = (
    "the gate-prod receipt store (~/.claude/state/gate-prod/) is written only by "
    "HRSE2's scripts/gate_production_run.py; a consumed receipt is what makes one "
    "AE one production run (harmonic-forge#878, R-0377). No tool call may read, "
    "change or remove it -- a second run needs a fresh AE with --prod-run."
)


def _under_store(raw: str) -> bool:
    try:
        path = Path(os.path.expanduser(raw))
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return True  # fail closed on a path we cannot reason about
    store = STORE.resolve(strict=False)
    return resolved == store or store in resolved.parents


def denial_reason(tool_name: str, tool_input: dict) -> str | None:
    if tool_name == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str):
            return REASON  # fail closed on a malformed payload
        if _NAMES_STORE.search(command) and _NAMES_STATE.search(command):
            return REASON
        return None
    key = _PATH_TOOLS.get(tool_name)
    if key is None:
        return None
    raw = tool_input.get(key)
    if not isinstance(raw, str):
        return None
    return REASON if _under_store(raw) else None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    reason = denial_reason(payload.get("tool_name", ""), payload.get("tool_input") or {})
    if reason:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
