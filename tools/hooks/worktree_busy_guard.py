#!/usr/bin/env python3
"""PreToolUse guard: no writes into a worktree whose own check is running
(harmonic-forge#1560, the enforcement of R-0376).

`mise run check` and `mise run local-check` hold a per-worktree `flock` for
the whole task (`tools/worktree/busy_lock.py`). A check that reads files the
session is still editing records a receipt for a HEAD the tested files no
longer match, so while a worktree is busy this hook denies:

- a Write/Edit/MultiEdit/NotebookEdit whose target path resolves under it;
- a Bash command whose text names its absolute path (followed by `/`, a
  quote, whitespace, `;`, `)` or the end of the text), unless the whole
  command is a read: `precheck_store_guard._read_only`, the R-0358
  cross-family call shape (`deny_advisory_subagent_gh_writes`), or a bare
  `git -C <W>` read verb (status/log/diff/show/rev-parse/ls-files), alone or
  as `cd <W> && git <verb>`.

Bash is judged by its command text only, never by the payload's `cwd`.
Residuals, stated: a script file elsewhere that writes into W with no W path
in its command text passes; so does a cwd-relative write from a session
whose cwd is W; so does a spelling of W other than its realpath (the
`~/harmonic-forge` symlink).

This hook fails OPEN. Any exception, an import failure or unreadable state
allows the call (reported on stderr). Only the deny path can block.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
_FILE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
_PATH_KEYS = ("file_path", "notebook_path")
_GIT_VERBS = "status|log|diff|show|rev-parse|ls-files"
_GIT_ARGS = r"(?: [^-][^ ]*| --[a-z-]+)*"
#: Anything that can redirect, substitute, pipe, background or chain. The
#: one `&&` the git allowlist accepts is split off before this is applied.
_NOT_PLAIN = re.compile(r"[>|&;`\n]|<\(|\$\(")


def _modules():
    sys.path.insert(0, str(HERE))
    sys.path.insert(0, str(HERE.parent / "worktree"))
    import busy_lock  # noqa: E402
    import deny_advisory_subagent_gh_writes as advisory  # noqa: E402
    import precheck_store_guard  # noqa: E402
    return busy_lock, advisory, precheck_store_guard


def names_root(command: str, root: str) -> bool:
    return re.search(re.escape(root) + r"(?=/|['\"]|\s|;|\)|\Z)", command) is not None


def _git_read(command: str, root: str) -> bool:
    if command.count("&") not in (0, 2) or (command.count("&") == 2 and "&&" not in command):
        return False
    parts = command.split("&&")
    if len(parts) > 2 or any(_NOT_PLAIN.search(part) for part in parts):
        return False
    if re.search(r"(?:^|\s)--output(?:=|\s|$)", command):
        return False
    w = re.escape(root)
    if len(parts) == 1:
        return re.fullmatch(rf"git -C {w} (?:{_GIT_VERBS}){_GIT_ARGS}", parts[0].strip()) is not None
    return (re.fullmatch(rf"cd {w}", parts[0].strip()) is not None
            and re.fullmatch(rf"git (?:{_GIT_VERBS}){_GIT_ARGS}", parts[1].strip()) is not None)


def _cross_family(command: str, advisory) -> bool:
    if _NOT_PLAIN.search(command) or "<" in command:
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    argv = advisory._cross_family_argv(tokens)
    return argv is not None and advisory._cross_family_permitted(argv)


def bash_allowed(command: str, root: str, advisory, precheck_store_guard) -> bool:
    """A command that names a busy root passes only if the whole of it is a read."""
    return (precheck_store_guard._read_only(command)
            or _cross_family(command, advisory)
            or _git_read(command, root))


def _deny(root: str, busy_lock) -> str:
    record = busy_lock.read_sidecar(root) or {}
    pid = record.get("shell_pid", "?")
    return (f"worktree busy guard (harmonic-forge#1560): {root} -- a full check is running in "
            f"this worktree (R-0376); wait for it, or stop it with the task-stop tool or "
            f"`kill {pid}`, then edit. Check started {record.get('started_at', '?')} at head "
            f"{record.get('head', '?')}, shell_pid {pid}.")


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def guard(payload: dict, modules=None) -> str | None:
    """The deny reason, or None."""
    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict) or (tool not in _FILE_TOOLS and tool != "Bash"):
        return None
    busy_lock, advisory, precheck_store_guard = modules or _modules()
    roots = busy_lock.busy_roots()
    if not roots:
        return None
    if tool in _FILE_TOOLS:
        raw = next((str(tool_input[k]) for k in _PATH_KEYS if tool_input.get(k)), "")
        if not raw:
            return None
        base = str(payload.get("cwd") or os.getcwd())
        target = os.path.realpath(os.path.join(base, os.path.expanduser(raw)))
        for root in roots:
            if _under(target, root):
                return _deny(root, busy_lock)
        return None
    command = str(tool_input.get("command") or "")
    for root in roots:
        if names_root(command, root) and not bash_allowed(command, root, advisory,
                                                          precheck_store_guard):
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
