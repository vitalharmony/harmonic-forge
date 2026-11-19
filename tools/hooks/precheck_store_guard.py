#!/usr/bin/env python3
"""PreToolUse guard for the precheck gate's own store (harmonic-forge#1291,
design alternative 8).

Receipts, refusal records and the decision log live in
`~/.claude/state/precheck-gate/`, which the agent could otherwise write. This
hook denies any Bash/Write/Edit/MultiEdit/NotebookEdit call that names that
directory (however spelled: `precheck-gate/`, `precheck_gate/`), or that names
`gate_receipt` together with `record` (an inline `python3 -c`, a heredoc, an
`import gate_receipt`): only a gated tool's own precheck form writes a receipt.

Reads are not writes: a Bash command whose whole text is a read (`cat`,
`less`, `grep`/`egrep`/`fgrep`, `jq`, `ls`, or `rg` with no `--pre`/
`--pre-glob`) passes, with no redirection, chaining or substitution. File
tools are judged by the path they write, never by their content, so editing
this hook, the gate or their tests is not denied.

The guard raises the floor and claims nothing more: like the FORCE and IMC
grant stores, the store is an audit trail, not proof against a session that
can write it. A shell spelling the patterns miss, or a process that edits a
tool file, defeats it.
"""
from __future__ import annotations

import json
import re
import sys

_FILE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
_PATH_KEYS = ("file_path", "notebook_path", "path")
#: The store directory, not the hook file `precheck_gate.py`: a separator,
#: quote, blank or the end must follow the name.
_STORE_PATH = re.compile(r"precheck[-_]gate(?=/|$|[\s'\"`;)|&])", re.I)
_RECORD_CALL = re.compile(r"gate_receipt", re.I)
_RECORD_WORD = re.compile(r"\brecord\b", re.I)
_READ_COMMANDS = ("cat", "less", "grep", "egrep", "fgrep", "ls", "jq", "rg")
_NOT_A_READ = re.compile(r"[;&`>]|\$\(|<\(|\btee\b|\n")
_RG_PRE = re.compile(r"(?:^|\s)--pre(?:-glob)?(?:=|\s|$)")


def _strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _read_only(command: str) -> bool:
    if _NOT_A_READ.search(command):
        return False
    for segment in (s.strip() for s in command.split("|")):
        program = segment.split(" ", 1)[0] if segment else ""
        if program not in _READ_COMMANDS:
            return False
        if program == "rg" and _RG_PRE.search(segment):
            return False
    return True


def guard(payload: dict) -> str | None:
    """The deny reason, or None."""
    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input")
    if tool in _FILE_TOOLS and isinstance(tool_input, dict):
        text = "\n".join(str(tool_input.get(k) or "") for k in _PATH_KEYS)
        if _STORE_PATH.search(text):
            return _deny()
        return None
    text = "\n".join(_strings(tool_input))
    if tool in ("Bash", "Monitor", "shell") and isinstance(tool_input, dict) \
            and _read_only(str(tool_input.get("command") or "")):
        return None
    if _STORE_PATH.search(text) or (_RECORD_CALL.search(text) and _RECORD_WORD.search(text)):
        return _deny()
    return None


def _deny() -> str:
    return ("precheck store guard (harmonic-forge#1291): this call names the precheck gate's "
            "store (~/.claude/state/precheck-gate/) or writes a receipt through gate_receipt. "
            "The only writer of a receipt is the tool's own precheck form: run it. Reads "
            "(cat, less, grep, jq, ls, rg without --pre) are allowed.")


def main() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - an unreadable payload decides nothing
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        reason = guard(payload)
    except Exception as exc:  # noqa: BLE001 - fail closed on the guard
        reason = f"precheck store guard could not inspect this call ({type(exc).__name__}); denied"
    if reason:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
