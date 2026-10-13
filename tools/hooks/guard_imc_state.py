#!/usr/bin/env python3
"""PreToolUse: no tool call may name the IMC state directory
(harmonic-forge#895, R-0384).

`~/.claude/state/imc/` holds the IMC hook records, which only
`expand_lane_shorthand.py` writes on `UserPromptSubmit`, and the waiver
receipts, which only `tools/gh/imc_waiver.py record` writes after the waiver
comment is posted and refetched. A session that could write either could waive
its own review.

Default-deny on the PATH, not an allowlist of write shapes: that allowlist is
what `guard_gh_rest_budget.py` had to be reforged away from (harmonic-forge#650
-- it missed `mkdir -p`, `env touch` and `pathlib` writes). Modelled on
`auto_ae_toggle.py`'s guard: a file tool (`Write`, `Edit`, `MultiEdit`,
`NotebookEdit`) is judged only on the path it writes, never its content (this
file's own source names the directory); Codex `apply_patch` on its
`*** ... File:` lines; everything else on its whole text. Unlike auto-AE, no
read is exempt: the one exception is the exact command

    python3 tools/gh/imc_waiver.py record --key <K>

which names no state path itself.

THIS IS A MISTAKE-DETECTOR, NOT A BOUNDARY. A path composed indirectly
(`'im' + 'c'`, a shell variable) passes, and every lane runs as the same user.
"""
from __future__ import annotations

import json
import re
import sys

_FILE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
_PATH_KEYS = ("file_path", "notebook_path", "path")
_PATCH_PATH = re.compile(r"(?m)^\*\*\* (?:(?:Add|Update|Delete) File|Move to):[ \t]*(.+)$")
#: `state/imc`, however joined: a slash path, or pathlib/os.path segments
#: (`'state' / 'imc'`, `"state", "imc"`).
_PROTECTED = re.compile(r"""state\s*['"]?\s*[/,]\s*['"]?imc(?![A-Za-z0-9_-])""", re.I)
#: A command that loads `imc_waiver` and calls one of its state writers writes
#: the state without naming the directory (preclose pass 1, cross-family).
_MODULE_WRITE = re.compile(
    r"imc_waiver.*?(write_json_atomic|receipt_path|hook_record_path|\.unlink|write_text|open\()",
    re.S)
#: Any invocation of the record command must be exactly the allowed form.
_RECORD_CALL = re.compile(r"imc_waiver\.py[ \t]+record\b")
#: The single exception. Whole command, nothing chained.
_ALLOWED = re.compile(
    r"python3[ \t]+(?:\S*/)?tools/gh/imc_waiver\.py[ \t]+record[ \t]+--key[ \t]+[A-Za-z]\d{1,6}")
_REASON = ("IMC guard (R-0384): this call names the IMC state directory. Only the "
           "UserPromptSubmit hook writes its records, and only `python3 tools/gh/imc_waiver.py "
           "record --key <K>` writes a waiver receipt.")


def _strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _normalize(text: str) -> str:
    """Collapse `//`, `/./` and `seg/../` so a path that resolves into the
    state directory is matched however it is spelled (preclose pass 1)."""
    text = re.sub(r"/{2,}", "/", text)
    previous = None
    while previous != text:
        previous = text
        text = re.sub(r"/\./", "/", text)
        text = re.sub(r"/[^/\s'\"]+/\.\./", "/", text)
    return text


def allowed_command(command: str) -> bool:
    return _ALLOWED.fullmatch(command.strip()) is not None


def guard(payload: dict) -> str | None:
    """The deny reason, or None to let the call through."""
    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input")
    if tool in _FILE_TOOLS and isinstance(tool_input, dict):
        text = "\n".join(str(tool_input.get(k) or "") for k in _PATH_KEYS)
    elif tool == "apply_patch":
        whole = "\n".join(_strings(tool_input))
        paths = [m.group(1) for m in _PATCH_PATH.finditer(whole)]
        text = "\n".join(paths) if paths else whole
    else:
        if tool in ("Bash", "Monitor", "shell") and isinstance(tool_input, dict) \
                and allowed_command(str(tool_input.get("command") or "")):
            return None
        text = "\n".join(_strings(tool_input))
        if _MODULE_WRITE.search(text) or _RECORD_CALL.search(text):
            return _REASON
    return _REASON if _PROTECTED.search(_normalize(text)) else None


def main() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - an unreadable payload decides nothing
        return 0
    if not isinstance(payload, dict):
        return 0
    event = payload.get("hook_event_name")
    if event not in ("PreToolUse", None) or "tool_name" not in payload:
        return 0
    try:
        reason = guard(payload)
    except Exception as exc:  # noqa: BLE001 - fail closed on the guard
        reason = f"IMC guard could not inspect this call ({type(exc).__name__}); denied"
    if reason:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
