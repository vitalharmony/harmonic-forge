#!/usr/bin/env python3
"""PreToolUse hook: a data-migration issue must be labelled executed to close.

hrse#859. The Lane 1 -> Lane 2 -> Lane 3 loop asserts that code is
correct; nothing asserts that a *migration* actually executed. When an
issue's scope IS a data migration, merging the code satisfies every
check that exists, so closing it looks legitimate while the data is
untouched.

Real incident, hrse#849 (2026-08-13): the classifier fix merged
(hrse#853, 6a2d907) and the issue was closed while all 219
null-direction Activity rows were still null. Its own closing comment
said the run was "still outstanding". It blocked hrse#847 and hrse#856
for as long as it read `closed`, and was reopened by hand only because
a human noticed in conversation.

## What this guarantees, stated exactly

Closing a `data-migration` issue through `gh issue close` or a
`gh api PATCH ... state=closed` requires a deliberate second action --
applying the `migration-executed` label -- rather than happening as a
side effect of merging code. **It does not and cannot prevent the
close.** The hook is fail-open, and it sees nothing of `gh api graphql`
mutations, `--input -`/heredoc bodies, the GitHub web UI, or a close
issued from Python or curl. Its audience is the honest-but-careless
agent; it has never constrained a dishonest one.

The load-bearing control *will be* the after-the-fact sweep for closed
`data-migration` issues lacking `migration-executed` -- filed as
hrse#867 and **not yet shipped**. That catches every close path this
hook structurally cannot see. Until it lands, this hook is the only
control, and it is the half that both documents describe as *not*
load-bearing. Do not mistake it for coverage.

## Why a label, not a marker in a comment

Four review rounds rejected comment-parsing designs, and three failed
the same way: prose treated as a credential. A marker's format must be
published -- in `3-lane-protocol.md`, `rules/testing-gate.md`, this
file's own deny message, and the companion issue hrse#866 -- so **every
published example is itself a valid credential**. Round 4 proved it:
hrse#866's body carries a fenced example receipt naming hrse#849, and
pasting that body onto the thread opened the gate for the exact issue
this hook was built to protect. Successive rounds narrowed where an
example may legally appear (not blockquoted, then not fenced, then not
indented) -- a blocklist maintained against your own documentation,
which does not converge.

A label ends the class rather than narrowing it: naming the label is
not applying it, so the docs may quote the mechanism freely. Label
application is also a timeline event carrying actor and timestamp,
which a pasted comment is not.

`model_tier_gate.py` is the only other hook here that reads GitHub
state to decide, and it reads a structured board field for the same
reason.

Known residual gaps, recorded rather than left implicit: `gh api
graphql` mutations, `--input -`/heredoc bodies, `xargs`-style
indirection, and any close not issued through the Bash tool.
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

LABEL = "data-migration"
EXECUTED_LABEL = "migration-executed"
ABANDONED_LABEL = "migration-abandoned"

API_ISSUE_PATH = re.compile(r"(?:^|/)repos/([\w.-]+/[\w.-]+)/issues/(\d+)(?:/|$)")
ISSUE_URL = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)")
ENV_ASSIGNMENT = re.compile(r"^\w+=")

ISSUE_NUMBER = re.compile(r"^#?(\d+)$")

REPO_FLAGS = ("--repo", "-R")


def _allow() -> None:
    print(json.dumps({}))


def _deny(reason: str) -> None:
    # permissionDecisionReason is what the *model* sees. systemMessage is
    # only surfaced to the user, so a hook setting solely the latter
    # reports "Blocked by hook" to the agent -- which is precisely the
    # agent that then retries with a variant flag order.
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
        "systemMessage": reason,
    }))


def _gh(*args: str, cwd: str | None = None) -> str | None:
    """Return stdout, or None on any failure.

    Fail-open by design: a hook that blocks work whenever the network is
    slow trains people to disable it, and a disabled hook enforces
    nothing.
    """
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
    """Value of `--name value` or `--name=value` at tokens[index]."""
    token = tokens[index]
    for name in names:
        if token == name and index + 1 < len(tokens):
            return tokens[index + 1]
        if token.startswith(f"{name}="):
            return token.split("=", 1)[1]
    return None


def _parse_issue_close(tokens: list[str]) -> tuple[str | None, str] | None:
    """Parse `gh issue close ...` -> (repo_or_None, issue)."""
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
            # Skip a token that is a preceding flag's value.
            previous = rest[i - 1] if i else ""
            if previous.startswith("-") and "=" not in previous:
                continue
            # `gh issue close` accepts {<number> | <url> | <branch>}.
            url = ISSUE_URL.search(token)
            if url:
                repo, issue = url.group(1), url.group(2)
                continue
            match = ISSUE_NUMBER.match(token)
            if match:
                issue = match.group(1)

    return (repo, issue) if issue else None


def _parse_api_close(tokens: list[str]) -> tuple[str, str] | None:
    """Parse `gh api ... PATCH ... state=closed` -> (repo, issue)."""
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
        # shlex has already stripped quotes, so `state='closed'` and
        # `state=closed` both arrive here identically.
        if token.replace(" ", "") == "state=closed":
            closes = True
        match = API_ISSUE_PATH.search(token)
        if match:
            target = (match.group(1), match.group(2))

    return target if (is_patch and closes and target) else None


def _strip_invocation_prefix(tokens: list[str]) -> list[str]:
    """Drop `env` and leading VAR=value assignments before the program."""
    index = 0
    while index < len(tokens) and (
        tokens[index] == "env" or ENV_ASSIGNMENT.match(tokens[index])
    ):
        index += 1
    return tokens[index:]


def find_close_targets(command: str) -> list[tuple[str | None, str]] | None:
    """All (repo, issue) pairs this command would close.

    Returns None when the command cannot be tokenized, treated as
    fail-open by the caller.

    Segmentation uses the shared `shell_parse.command_segments`, which
    every sibling hook uses (harmonic-forge#167). Two earlier attempts
    failed here: a regex with a greedy optional skip group bound
    `gh issue close 849 --comment 'closes 219 rows'` to issue *219*, and
    a private regex segmenter split on `;`/`|` inside quoted arguments.
    """
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
    """Repo for the command, falling back to the cwd's own remote.

    The bare `gh issue close N` form is how issues are actually closed,
    so treating a missing --repo as unresolvable would leave the most
    common path unguarded. Resolution runs in the *command's* directory:
    the payload carries `cwd`, and resolving against the hook process's
    own directory checked the wrong repository's issue #N.
    """
    if explicit:
        return explicit
    out = _gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner",
              cwd=cwd)
    return out.strip() if out and out.strip() else None


def labels_for(repo: str, issue: str) -> set[str] | None:
    """Label names on the issue, or None when they cannot be read."""
    out = _gh("api", f"repos/{repo}/issues/{issue}", "--jq", ".labels[].name")
    if out is None:
        return None
    return {line.strip() for line in out.splitlines() if line.strip()}


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

        labels = labels_for(repo, issue)
        if labels is None:  # fail-open, same rationale as _gh
            continue
        if LABEL not in labels:
            continue
        if EXECUTED_LABEL in labels or ABANDONED_LABEL in labels:
            continue

        _deny(
            f"Blocked: {repo}#{issue} is labelled {LABEL!r} and carries "
            f"neither {EXECUTED_LABEL!r} nor {ABANDONED_LABEL!r}, so nothing "
            f"records that the migration ran (hrse#859).\n\n"
            f"Merging the code is not running the migration. hrse#849 closed "
            f"exactly this way on 2026-08-13 with all 219 rows untouched, and "
            f"blocked two downstream issues until a human caught it.\n\n"
            f"Run the migration, then:\n"
            f"  gh issue edit {issue} --repo {repo} --add-label "
            f"{EXECUTED_LABEL}\n\n"
            f"If it is being deliberately abandoned, apply {ABANDONED_LABEL!r} "
            f"instead and say why in a comment.\n\n"
            f"This hook only makes the close require a deliberate labelling "
            f"action -- it cannot prevent one, and does not see graphql "
            f"mutations, heredoc bodies, or web-UI closes."
        )
        return

    _allow()


if __name__ == "__main__":
    main()
