#!/usr/bin/env python3
"""Refuse a new feedback memory that restates an existing rule (harmonic-forge#981).

PreToolUse on `Write|Edit|MultiEdit`. When a lane breaks protocol and is
corrected, the session tends to write a new `feedback_*.md` restating a rule
that already exists, instead of re-reading the rule and recording why it did
not fire (`tools/memory/capture_failure.py`, harmonic-forge#520). This hook
gates the CREATION of a feedback memory under the configured store on a
`rule_check:` frontmatter declaration:

* `rule_check: R-NNNN` that resolves in a rule registry -> deny, with the
  `capture_failure.py` command and the grep for the memory already pointing at
  that rule. There is no override.
* `rule_check: none (searched: <terms>)` -> allow, unless the file's own body
  cites an R-ID that resolves (operator ruling 2, 2026-10-13: the one checkable
  half of a "none" declaration).
* missing, or an `R-NNNN` that does not resolve -> deny, naming both forms.

What counts as creation: the path does not exist yet (including an `Edit` whose
`old_string` is empty, which creates the file), or a `Write` replaces an
existing file that was not a feedback memory. Edits of an existing feedback
memory pass, and so do `user`/`project`/`reference` memories. Any parse or
resolution failure exits silently: this hook gates an agent convenience, and a
crash here must never block an unrelated write.

A Bash heredoc into the store is not seen here. `memory_lint.py`'s rule_check
check (AC6) reports such a file afterwards.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "memory"))

import memory_lint  # noqa: E402

TOOLS = {"Write", "Edit", "MultiEdit"}
_RULE_ID = re.compile(r"\bR-\d{4}\b")
_RULE_CHECK = re.compile(r"^rule_check:\s*(.*?)\s*$", re.MULTILINE)
_NONE_FORM = re.compile(r"^none\s*\(\s*searched:\s*[^)\s][^)]*\)\s*$", re.IGNORECASE)

#: The forge registry, resolved next to this file so it works in any checkout,
#: plus HRSE2's sibling registry (a disjoint ID set, `check_cross_registry.py`).
REGISTRIES = (
    _HERE.parent / "rules" / "registry.toml",
    Path.home() / "Harmonic_Projects/HRSE2/.claude/rules/registry.toml",
)


def rule_ids() -> set[str]:
    ids: set[str] = set()
    for reg in REGISTRIES:
        try:
            text = reg.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        ids |= set(re.findall(r'^id\s*=\s*"(R-\d{4})"', text, re.MULTILINE))
    return ids


def _frontmatter_block(text: str) -> str:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            return "\n".join(lines[1:i])
    return ""


def is_feedback(path: Path, text: str) -> bool:
    return memory_lint.is_feedback_memory(path.name, text)


def _in_store(path: Path, store: Path) -> bool:
    try:
        path.resolve().relative_to(store.resolve())
    except (ValueError, OSError):
        return False
    return True


def new_text(tool: str, tool_input: dict, path: Path) -> str | None:
    """The file's text after this call, or None when the call creates nothing."""
    exists = path.exists()
    if tool == "Write":
        content = str(tool_input.get("content", ""))
        if not exists:
            return content
        try:
            old = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        return None if is_feedback(path, old) else content
    if exists:
        return None
    if tool == "Edit":
        if tool_input.get("old_string", "") == "":
            return str(tool_input.get("new_string", ""))
        return None
    if tool == "MultiEdit":
        edits = tool_input.get("edits") or []
        if not edits or edits[0].get("old_string", "") != "":
            return None
        # Apply every edit in order: the text checked must be the text written
        # (preclose pass 1: a later edit could otherwise swap the declaration).
        text = str(edits[0].get("new_string", ""))
        for edit in edits[1:]:
            old_s, new_s = str(edit.get("old_string", "")), str(edit.get("new_string", ""))
            if not old_s:
                continue
            text = text.replace(old_s, new_s) if edit.get("replace_all") else text.replace(old_s, new_s, 1)
        return text
    return None


def _store_grep(store: Path, rid: str) -> str:
    return f'grep -l "{rid}" {store}/feedback_*.md'


def _restates(rid: str, store: Path, why: str) -> str:
    return (
        f"Blocked (harmonic-forge#981): this new feedback memory {why} {rid}, "
        "an existing rule. A rule that exists and did not fire needs its failure "
        "recorded, not another memory. Instead:\n"
        f"  1. quote the rule: python3 ~/harmonic-forge/tools/rules/query_rules.py --id {rid}\n"
        "  2. record the failure: python3 ~/harmonic-forge/tools/memory/capture_failure.py "
        f'--caught-by <operator|self|hook> --rule-existed rule --rule-ref {rid} '
        '--expected "…" --happened "…"\n'
        "  3. raise `instances:` on the memory already pointing at that rule "
        f"({_store_grep(store, rid)}) instead of writing a new one.\n"
        "There is no override. Writing the same memory through Bash is a protocol "
        "violation the memory fold reports."
    )


_SEARCH = ('grep -rn -i "<term>" ~/harmonic-forge/rules ~/harmonic-forge/3-lane-protocol.md '
           "~/harmonic-forge/tools/rules/registry.toml")


def _missing(detail: str) -> str:
    return (
        f"Blocked (harmonic-forge#981): a new feedback memory needs a `rule_check:` "
        f"frontmatter line ({detail}). Search the rules first: {_SEARCH}\n"
        "Then declare one of:\n"
        "  rule_check: R-NNNN                  (the rule exists: you will be routed to "
        "capture_failure.py instead)\n"
        "  rule_check: none (searched: <terms>)  (no rule covers it: the write is allowed)"
    )


def decide(tool: str, tool_input: dict, store: Path, ids: set[str]) -> str | None:
    """The deny message, or None to allow."""
    if tool not in TOOLS:
        return None
    raw = tool_input.get("file_path")
    if not raw:
        return None
    path = Path(str(raw)).expanduser()
    if not _in_store(path, store):
        return None
    text = new_text(tool, tool_input, path)
    if text is None or not is_feedback(path, text):
        return None
    m = _RULE_CHECK.search(_frontmatter_block(text))
    if not m or not m.group(1):
        return _missing("none found")
    value = m.group(1).strip().strip("\"'")
    if value.lower().startswith("none") and not _NONE_FORM.match(value):
        return _missing(f"`{value}` names no search; write `none (searched: <terms>)`")
    if _NONE_FORM.match(value):
        body = memory_lint._body_text(text)
        cited = [rid for rid in dict.fromkeys(_RULE_ID.findall(body)) if rid in ids]
        if cited:
            return _restates(cited[0], store, "declares `rule_check: none` but its body cites")
        return None
    rid = value.split()[0]
    if rid in ids:
        return _restates(rid, store, "declares `rule_check:`")
    return _missing(f"`{value}` is not a rule ID in any registry")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        tool = payload.get("tool_name", "")
        if tool not in TOOLS:
            return 0
        message = decide(tool, payload.get("tool_input") or {},
                         memory_lint.resolve_store(), rule_ids())
    except Exception:  # noqa: BLE001 -- never block an unrelated write on a hook fault
        return 0
    if message:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": message,
        }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
