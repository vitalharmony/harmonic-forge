#!/usr/bin/env python3
"""PreToolUse guard: no file-tool writes into a worktree whose own check is
running (harmonic-forge#1560, the enforcement of R-0376).

`mise run check` and `mise run local-check` hold a per-worktree `flock` for
the whole task (`tools/worktree/busy_lock.py`). While a worktree is busy this
hook denies a Write/Edit/MultiEdit/NotebookEdit whose target path resolves
under it: real argv, a real path and realpath containment.

Bash is always allowed (design amendment issuecomment-7169440744, AC3). A Bash
write into a busy worktree is caught instead by the check's own integrity step
(`busy_lock.py verify`), which fails the check with no receipt: shell text is
a grammar this repo does not own, so predicting a write from it cannot
converge (sticky-wicket verdict issuecomment-7156806265).

This hook fails OPEN. Any exception, an import failure or unreadable state
allows the call (reported on stderr). Only the deny path can block.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
_FILE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
_PATH_KEYS = ("file_path", "notebook_path")


def _busy_lock():
    sys.path.insert(0, str(HERE.parent / "worktree"))
    import busy_lock  # noqa: E402
    return busy_lock


def _deny(root: str, busy_lock) -> str:
    record = busy_lock.read_sidecar(root) or {}
    pid = record.get("shell_pid", "?")
    return (f"worktree busy guard (harmonic-forge#1560): {root} -- a full check is running in "
            f"this worktree (R-0376); wait for it, or stop it with the task-stop tool or "
            f"`kill -- -{pid}`, then edit. Check started {record.get('started_at', '?')} at head "
            f"{record.get('head', '?')}, shell_pid {pid}.")


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def guard(payload: dict, busy_lock=None) -> str | None:
    """The deny reason, or None."""
    tool = str(payload.get("tool_name") or "")
    if tool == "Bash" or tool not in _FILE_TOOLS:
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    busy_lock = busy_lock or _busy_lock()
    roots = busy_lock.busy_roots()
    if not roots:
        return None
    raw = next((str(tool_input[k]) for k in _PATH_KEYS if tool_input.get(k)), "")
    if not raw:
        return None
    base = str(payload.get("cwd") or os.getcwd())
    target = os.path.realpath(os.path.join(base, os.path.expanduser(raw)))
    for root in roots:
        if _under(target, root):
            return _deny(root, busy_lock)
    return None


def main() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        reason = guard(payload) if isinstance(payload, dict) else None
    except Exception as exc:  # noqa: BLE001 - fail OPEN: only the deny path may block
        print(f"worktree busy guard: allowed, could not inspect this call "
              f"({type(exc).__name__}: {exc})", file=sys.stderr)
        return 0
    if reason:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
