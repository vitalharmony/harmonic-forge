#!/usr/bin/env python3
"""PreToolUse hook: a Tooling-Exception close needs a posted preclose-inspection.

hrse#1487. CLAUDE.md requires `preclose-inspection` "AFTER Tooling Exception
work is implemented and BEFORE Lane 1 requests closure" -- but until this
hook, that was prose-only. Every other closure-adjacent rule that has
actually held under pressure in this repo (auto-close keyword syntax,
Lane 1/2 status claims, AE self-post, the data-migration close gate this
hook is modeled on) is enforced by a PreToolUse hook that blocks the Bash
call outright.

Real incident, hrse#1476 (2026-09-01): Lane 1 implemented, verified, and
pushed a Tooling Exception PR, then moved straight to merge/close --
skipping preclose-inspection entirely. It only ran because the operator
asked "isn't preclose inspection required?" after the fact. Re-run for
real, it found 5 issues that would otherwise have merged, including one
(a dead `if:` condition) that would have shipped the whole feature inert
and green forever.

## What this guarantees, stated exactly

Closing an issue via `gh issue close` or a `gh api PATCH ... state=closed`
requires a posted `preclose-inspection` findings comment first, UNLESS the
issue already shows a real Lane 2/Lane 3 gate trail (a `ready-for-l3`, `ae`,
or `ae-and-sweep` marker from `l1_post.py` -- i.e. it went through the
3-lane cycle, where a different gate already covers this). It does not
label-gate, unlike `block_data_migration_close.py`'s `tooling-exception`
label: hrse#1476 itself was never labelled `tooling-exception`, so a
label-based check would not have caught the incident it exists to prevent.
The gate trail check is what `l1_post.py` already enforces mechanically,
not a human-applied label that can be forgotten.

**It does not and cannot prevent the close.** Fail-open by design, same
rationale as every sibling hook here: it sees nothing of `gh api graphql`
mutations, heredoc bodies, the GitHub web UI, or a close issued from Python
or curl. Its audience is the honest-but-careless agent.

## The marker

A `preclose-inspection` findings comment is any issue comment whose body
contains a heading matching `^#{1,4}\\s*preclose-inspection` (case
insensitive), posted via `mise run lane-comment`. No attestation machinery
(SHA, body hash) -- preclose-inspection is advisory, not a status claim
about GitHub/live state, so it does not belong in `l1_post.py`'s kind set.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from shell_parse import command_segments  # noqa: E402

GATE_TRAIL_MARKER = re.compile(r"kind=(?:ready-for-l3|ae|ae-and-sweep)\b")
PRECLOSE_HEADING = re.compile(r"(?im)^#{1,4}\s*preclose-inspection\b")

API_ISSUE_PATH = re.compile(r"(?:^|/)repos/([\w.-]+/[\w.-]+)/issues/(\d+)(?:/|$)")
ISSUE_URL = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)")
ENV_ASSIGNMENT = re.compile(r"^\w+=")
ISSUE_NUMBER = re.compile(r"^#?(\d+)$")
REPO_FLAGS = ("--repo", "-R")


def _allow() -> None:
    print(json.dumps({}))


def _deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
        "systemMessage": reason,
    }))


def _gh(*args: str, cwd: str | None = None) -> str | None:
    """Return stdout, or None on any failure. Fail-open, see module docstring."""
    if shutil.which("gh") is None:
        return None
    try:
        result = subprocess.run(
            ("gh", *args), capture_output=True, text=True, timeout=7, cwd=cwd,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    return result.stdout


def _flag_value(tokens: list[str], index: int, names: tuple[str, ...]) -> str | None:
    token = tokens[index]
    for name in names:
        if token == name and index + 1 < len(tokens):
            return tokens[index + 1]
        if token.startswith(f"{name}="):
            return token.split("=", 1)[1]
    return None


def _parse_issue_close(tokens: list[str]) -> tuple[str | None, str] | None:
    if len(tokens) < 4 or tokens[1] != "issue" or tokens[2] != "close":
        return None

    rest = tokens[3:]
    repo: str | None = None
    issue: str | None = None

    for i, token in enumerate(rest):
        value = _flag_value(rest, i, REPO_FLAGS)
        if value:
            repo = value
        if issue is None and not token.startswith("-"):
            previous = rest[i - 1] if i else ""
            if previous.startswith("-") and "=" not in previous:
                continue
            url = ISSUE_URL.search(token)
            if url:
                repo, issue = url.group(1), url.group(2)
                continue
            match = ISSUE_NUMBER.match(token)
            if match:
                issue = match.group(1)

    return (repo, issue) if issue else None


def _parse_api_close(tokens: list[str]) -> tuple[str, str] | None:
    if len(tokens) < 3 or tokens[1] != "api":
        return None

    joined = tokens[2:]
    is_patch = False
    closes = False
    target: tuple[str, str] | None = None

    for i, token in enumerate(joined):
        upper = token.upper()
        if upper in ("-XPATCH", "--METHOD=PATCH", "-X=PATCH"):
            is_patch = True
        if token in ("-X", "--method") and i + 1 < len(joined):
            if joined[i + 1].upper() == "PATCH":
                is_patch = True
        if token.replace(" ", "") == "state=closed":
            closes = True
        match = API_ISSUE_PATH.search(token)
        if match:
            target = (match.group(1), match.group(2))

    return target if (is_patch and closes and target) else None


def _strip_invocation_prefix(tokens: list[str]) -> list[str]:
    index = 0
    while index < len(tokens) and (
        tokens[index] == "env" or ENV_ASSIGNMENT.match(tokens[index])
    ):
        index += 1
    return tokens[index:]


def find_close_targets(command: str) -> list[tuple[str | None, str]] | None:
    try:
        segments = command_segments(command)
    except ValueError:
        return None

    targets: list[tuple[str | None, str]] = []
    for raw_tokens in segments:
        tokens = _strip_invocation_prefix(raw_tokens)
        if not tokens:
            continue
        if os.path.basename(tokens[0]) != "gh":
            continue
        parsed = _parse_issue_close(tokens) or _parse_api_close(tokens)
        if parsed:
            targets.append(parsed)
    return targets


def resolve_repo(explicit: str | None, cwd: str | None = None) -> str | None:
    if explicit:
        return explicit
    out = _gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner",
              cwd=cwd)
    return out.strip() if out and out.strip() else None


def comment_bodies(repo: str, issue: str) -> list[str] | None:
    out = _gh("api", f"repos/{repo}/issues/{issue}/comments",
              "--paginate", "--jq", ".[].body")
    if out is None:
        return None
    # Each comment body is one jq-emitted JSON string per line; comments
    # containing literal newlines still round-trip correctly since jq
    # escapes them within the string, so splitting on raw newlines here
    # would be wrong -- decode each line as its own JSON string instead.
    bodies = []
    for line in out.splitlines():
        try:
            bodies.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            bodies.append(line)
    return bodies


def has_gate_trail(bodies: list[str]) -> bool:
    return any(GATE_TRAIL_MARKER.search(b) for b in bodies)


def has_preclose_inspection(bodies: list[str]) -> bool:
    return any(PRECLOSE_HEADING.search(b) for b in bodies)


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        _allow()
        return

    if data.get("tool_name") != "Bash":
        _allow()
        return

    command = (data.get("tool_input") or {}).get("command", "")
    payload_cwd = data.get("cwd") or None

    targets = find_close_targets(command)
    if not targets:
        _allow()
        return

    resolved: dict[str | None, str | None] = {}
    for explicit_repo, issue in targets:
        if explicit_repo not in resolved:
            resolved[explicit_repo] = resolve_repo(explicit_repo, payload_cwd)
        repo = resolved[explicit_repo]
        if repo is None:
            continue

        bodies = comment_bodies(repo, issue)
        if bodies is None:  # fail-open, same rationale as _gh
            continue

        if has_gate_trail(bodies):
            continue  # went through the real 3-lane gate; a different check covers this
        if has_preclose_inspection(bodies):
            continue

        _deny(
            f"Blocked: {repo}#{issue} shows no Lane 2/Lane 3 gate trail "
            f"(no ready-for-l3/ae/ae-and-sweep marker), so this went the "
            f"Tooling Exception route -- and carries no posted "
            f"preclose-inspection findings comment (hrse#1487).\n\n"
            f"hrse#1476 merged exactly this way on 2026-09-01: implemented, "
            f"verified, pushed, and headed straight to merge/close, skipping "
            f"the review CLAUDE.md requires. Run preclose-inspection now, "
            f"post its findings as a comment with a heading matching "
            f"'## Preclose-inspection' via `mise run lane-comment`, then "
            f"retry the close.\n\n"
            f"This hook only makes the close require a deliberate posted "
            f"review -- it cannot prevent one, and does not see graphql "
            f"mutations, heredoc bodies, or web-UI closes."
        )
        return

    _allow()


if __name__ == "__main__":
    main()
