#!/usr/bin/env python3
"""Deny a Lane 3 session from posting its own AE authorization comment
(harmonic-forge#216).

`3-lane-protocol.md`'s `AE` rule requires operator/Lane-1 go-ahead before
Lane 3 executes any TC, posted as a durable issue comment — but nothing
mechanical stopped a Lane 3 session from writing that comment itself. Live
incident: hrse#706 (2026-08-10), a Claude-filled Lane 3 session, told "AE"
only in chat, posted `## AE H706 — approved, execute` itself
(`posted-by=LANE3`) and proceeded. A Codex-filled Lane 3 session on the
same issue, given the identical chat-only trigger, correctly refused.
Nothing mechanical distinguished the two.

This hook needs no comment history and no network — only this process's
own `LANE` env var (set once, at session launch, by
`tools/lane/lane3` — see `block_lane1_status_claims.py`'s own docstring
for why that makes it trustworthy) and the text of the comment body about
to be posted, both already locally available. That is what makes
PreToolUse enforcement feasible here where it wasn't for
`remind_gate_readiness_sweep.py`'s purpose (that hook's own docstring:
"the hook can't know a spec was approved without reading posted-comment
history").

Transport surface intercepted — deliberately broader than
`remind_gate_readiness_sweep.py`'s single `--file` check.
`block_lane1_status_claims.py`'s `is_direct_transport()` already confirms
raw `gh issue comment` remains legitimate for LANE=2/3 sessions
(harmonic-forge#190) — a write-side check that only covered the wrapper
tasks would be trivially bypassed by using raw `gh` instead, producing an
*unmarked* comment that reads as more legitimate under any provenance-based
check, not less:
1. `mise run lane-comment --file <path>` / `post_lane_discussion.py --file
   <path>` (HRSE2 and its lane worktrees).
2. `mise run post-comment --file <path>` / `tools/gh/post_comment.py
   --file <path>` (harmonic-forge itself — its own `post-comment` task has
   no lane-marking convention today, but this check needs none; it keys
   off this process's own LANE, not comment attribution).
3. `gh issue comment ... --body "<text>"` — inline text.
4. `gh issue comment ... --body-file <path>`.

Explicitly NOT covered: `gh api repos/.../issues/.../comments` raw
POST/PATCH calls carrying a `-f body=...`/`-F body=@file` field.
`is_direct_transport()` detects that shape structurally but no existing
hook in this file extracts body content from it — there is no established
parsing convention to follow, and this issue's own threat-model framing is
non-adversarial (catching an accidental self-authorization, not stopping a
deliberately dishonest session going out of its way to use an unusual
transport). A real, narrow, accepted gap, not silently dropped.

Does not resolve heredoc-substituted `--body` values (masked upstream by
`mask_heredoc_bodies`, same limitation `pr_body_autoclose_text` documents
in `block_lane1_status_claims.py`) — `--body-file`/`--file` are the
reliable paths, matching how every real posting in this session's own
history is actually done (write the body to a file first, then reference
it by path).

Fail-closed on structural payload malformation (non-string command,
unparseable shell) — matching `block_lane1_status_claims.py`'s posting-
control convention. Fail-open (no match, i.e. allow) on a `--file`/
`--body-file` path that can't be read — matching both
`remind_gate_readiness_sweep.py`'s and `pr_body_autoclose_text`'s existing
behavior for that specific failure mode. No network call exists anywhere
in this file, so there is no live-unreachable case to design a policy
around.
"""

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from shell_parse import command_segments  # noqa: E402  (harmonic-forge#167)

# Heading-anchored, not a bare substring search for "AE" — a substring
# match would false-positive on ordinary prose that merely mentions or
# quotes the trigger vocabulary (this very issue's own body quotes
# `## AE H706 — approved, execute` verbatim as an example). Every real
# posting on the hrse#706 thread, and `3-lane-protocol.md`'s own
# documented examples, use a heading line starting with "AE" immediately
# followed by an issue reference.
AE_HEADING = re.compile(r"(?m)^#{1,3}\s*AE\s+\S")

# A fenced ```...``` block's content still starts at column 0 -- the
# heading anchor alone does NOT distinguish "quoting the AE format inside
# a code fence" from "using it" (caught live by this file's own test
# suite: an early version of this hook denied a body that only *quoted*
# the AE format, exactly the class of false positive the heading anchor
# was supposed to avoid). Strip fenced regions before matching so a
# quoted example -- like this very issue's own body -- doesn't trigger a
# deny.
FENCED_BLOCK = re.compile(r"```.*?```", re.DOTALL)


def find_file_arg(args: list[str]) -> str | None:
    for index, token in enumerate(args):
        if token == "--file" and index + 1 < len(args):
            return args[index + 1]
        if token.startswith("--file="):
            return token.partition("=")[2]
    return None


def find_body_text(args: list[str], cwd: Path) -> str | None:
    """Return the outgoing comment body text for one command segment, or
    None if this segment doesn't post a comment body at all. Empty string
    is a valid, meaningful return (an explicitly empty --body)."""
    if len(args) >= 3 and args[0] == "mise" and args[1] == "run" and args[2] in {"lane-comment", "post-comment"}:
        file_arg = find_file_arg(args)
        return _read_file(file_arg, cwd) if file_arg else None
    if args and Path(args[0]).name == "post_lane_discussion.py":
        file_arg = find_file_arg(args)
        return _read_file(file_arg, cwd) if file_arg else None
    if len(args) >= 2 and args[0].startswith("python") and Path(args[1]).name in {"post_lane_discussion.py", "post_comment.py"}:
        file_arg = find_file_arg(args[1:])
        return _read_file(file_arg, cwd) if file_arg else None
    if len(args) >= 3 and args[0] == "gh" and args[1] == "issue" and args[2] == "comment":
        for index, token in enumerate(args):
            if token == "--body" and index + 1 < len(args):
                return args[index + 1]
            if token.startswith("--body="):
                return token.partition("=")[2]
            if token == "--body-file" and index + 1 < len(args):
                return _read_file(args[index + 1], cwd)
            if token.startswith("--body-file="):
                return _read_file(token.partition("=")[2], cwd)
    return None


def _read_file(file_arg: str, cwd: Path) -> str | None:
    """Read a --file/--body-file path, resolved against cwd if relative.
    Returns None (fail-open — no match) if the path can't be read."""
    path = Path(file_arg).expanduser()
    if not path.is_absolute():
        path = cwd / path
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def denial(message: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": message,
        },
        "systemMessage": message,
    }


def decision(command: object, cwd: Path) -> dict:
    if os.environ.get("LANE") != "3":
        return {}
    if not isinstance(command, str):
        return denial(
            "Blocked: malformed Bash hook payload; refusing to bypass the "
            "Lane 3 AE-self-authorization guard (harmonic-forge#216)."
        )
    try:
        segments = command_segments(command)
    except (AttributeError, TypeError, ValueError):
        return denial(
            "Blocked: malformed shell command; refusing to bypass the "
            "Lane 3 AE-self-authorization guard (harmonic-forge#216)."
        )
    for segment in segments:
        body = find_body_text(segment, cwd)
        if body is not None and AE_HEADING.search(FENCED_BLOCK.sub("", body)):
            return denial(
                "Blocked: this session was launched as Lane 3 (LANE=3) and "
                "is posting a comment that reads as an 'AE <issue>' "
                "authorization (harmonic-forge#216). AE must come from the "
                "operator or Lane 1, never from the executing Lane 3 "
                "session itself — see the hrse#706 incident this guard "
                "exists to prevent. If genuine operator approval exists, "
                "ask Lane 1 to relay it as a durable comment, then retry."
            )
    return {}


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        print("{}")
        return
    if payload.get("tool_name") != "Bash":
        print("{}")
        return
    command = (payload.get("tool_input") or {}).get("command", "")
    cwd = Path(payload.get("cwd") or Path.cwd())
    print(json.dumps(decision(command, cwd)))


if __name__ == "__main__":
    main()
