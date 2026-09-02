#!/usr/bin/env python3
"""PreToolUse hook: a Tooling-Exception merge/close needs preclose-inspection.

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

**Opt-in, on the `tooling-exception` label.** `gh pr merge`, `gh issue
close`, and `gh api PATCH ... state=closed` are blocked on an issue that
carries `tooling-exception` and does not carry `preclose-inspected`.
Everything else passes untouched.

The first two implementations gated on the *absence* of a signal -- no
Lane 3 gate trail meant "Tooling Exception by elimination." Its own
preclose-inspection review rejected that, and was right: the footprint was
all 192 open hrse issues plus 80 forge issues rather than a marked subset,
so an ordinary "not planned" close (14 on hrse since 2026-08-01) was denied
and told to inspect a diff that does not exist. The only escape was
applying `preclose-inspected`, which falsely asserts a review that never
ran and corrodes the one signal the hook depends on. A gate whose escape
hatch is a lie is worse than no gate.

`block_data_migration_close.py:292` -- the hook this one is modeled on --
is opt-in for exactly this reason (`if LABEL not in labels: continue`), and
the deviation from it was the defect, not the design.

`gh pr merge` is gated, not just the close: preclose-inspection reviews
"the diff that is about to be merged" (`agents/preclose-inspection.md`), so
a gate firing only on the close would enforce the review after the code had
already landed on main. For a PR, the gate resolves the issues that PR
would close and checks their labels -- a PR carries no labels that mean
anything here.

## Why a label, not a comment marker

The first implementation gated on a `## Preclose-inspection` heading in an
issue comment. Its review rejected that too: it reintroduced verbatim the
class `block_data_migration_close.py` spent four review rounds eliminating
-- **a marker whose format must be published is itself a valid credential
wherever it is published.** Concretely, `tools/gh/fetch_lane1_context.py:21`
contains the literal text `kind=ready-for-l3` in prose, and a fenced example
of the required heading matched the heading regex documenting it.

A label ends the class rather than narrowing it: naming `preclose-inspected`
is not applying it, so this docstring, the deny message, and the protocol
docs may all quote the mechanism freely. Label application is also a
timeline event carrying actor and timestamp, which a pasted comment is not.

**It does not and cannot prevent the merge or close.** Fail-open by design,
same rationale as every sibling hook here: it sees nothing of `gh api
graphql` mutations, heredoc bodies, the GitHub web UI, or a merge/close
issued from Python or curl. Its audience is the honest-but-careless agent.
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

PRECLOSE_LABEL = "preclose-inspected"
#: The opt-in signal. Only issues explicitly scoped to the Tooling Exception
#: are gated -- see "What this guarantees" above for why gating on the
#: *absence* of a Lane 3 trail was wrong.
TOOLING_EXCEPTION_LABEL = "tooling-exception"

#: `gh pr merge` flags that consume the following token. Every other flag is
#: valueless, so a bare number after it IS the PR/issue number. The inherited
#: "skip any token whose predecessor starts with -" heuristic silently dropped
#: the number in `gh pr merge --squash 1486`, because pr-merge's dominant flags
#: (--squash/--merge/--rebase/--admin/--delete-branch) take no value -- the gate
#: parsed to no target and allowed every such merge (hrse#1487 review finding 3).
VALUE_TAKING_FLAGS = frozenset({
    "--repo", "-R", "--comment", "-c", "--reason", "--body", "-b",
    "--body-file", "-F", "--subject", "-t", "--title", "--match-head-commit",
    "--author-email",
})

API_ISSUE_PATH = re.compile(r"(?:^|/)repos/([\w.-]+/[\w.-]+)/issues/(\d+)(?:/|$)")
ISSUE_URL = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)")
PR_URL = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
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


def _parse_positional_target(
    rest: list[str], url_pattern: re.Pattern[str],
) -> tuple[str | None, str | None]:
    """(repo, number) from `gh <cmd> <sub> ...`'s remaining tokens."""
    repo: str | None = None
    number: str | None = None

    for i, token in enumerate(rest):
        value = _flag_value(rest, i, REPO_FLAGS)
        if value:
            repo = value
        if number is None and not token.startswith("-"):
            previous = rest[i - 1] if i else ""
            # Only skip a token that is genuinely a preceding flag's VALUE.
            # Testing `previous.startswith("-")` alone drops the real number
            # after any valueless flag -- see VALUE_TAKING_FLAGS.
            if previous in VALUE_TAKING_FLAGS:
                continue
            url = url_pattern.search(token)
            if url:
                repo, number = url.group(1), url.group(2)
                continue
            match = ISSUE_NUMBER.match(token)
            if match:
                number = match.group(1)

    return repo, number


def _parse_issue_close(tokens: list[str]) -> tuple[str | None, str, str] | None:
    """`gh issue close ...` -> (repo, issue_number, kind='issue')."""
    if len(tokens) < 4 or tokens[1] != "issue" or tokens[2] != "close":
        return None
    repo, number = _parse_positional_target(tokens[3:], ISSUE_URL)
    return (repo, number, "issue") if number else None


def _parse_pr_merge(tokens: list[str]) -> tuple[str | None, str, str] | None:
    """`gh pr merge ...` -> (repo, pr_number, kind='pr').

    hrse#1487's own preclose-inspection: gating only the close enforces the
    review after the diff has already landed on main, which is the opposite
    of what `agents/preclose-inspection.md` specifies.
    """
    if len(tokens) < 3 or tokens[1] != "pr" or tokens[2] != "merge":
        return None
    repo, number = _parse_positional_target(tokens[3:], PR_URL)
    # `gh pr merge` with no positional argument merges the current branch's
    # PR. Resolving that needs the branch, which the payload does not carry
    # reliably -- fail open rather than guess at a different PR.
    return (repo, number, "pr") if number else None


def _parse_api_close(tokens: list[str]) -> tuple[str, str, str] | None:
    """`gh api ... PATCH ... state=closed` -> (repo, issue, kind='issue')."""
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

    if is_patch and closes and target:
        return (target[0], target[1], "issue")
    return None


def _strip_invocation_prefix(tokens: list[str]) -> list[str]:
    index = 0
    while index < len(tokens) and (
        tokens[index] == "env" or ENV_ASSIGNMENT.match(tokens[index])
    ):
        index += 1
    return tokens[index:]


def find_gated_targets(command: str) -> list[tuple[str | None, str, str]] | None:
    """All (repo, number, kind) this command would merge or close.

    Returns None when the command cannot be tokenized (fail-open).
    """
    try:
        segments = command_segments(command)
    except ValueError:
        return None

    targets: list[tuple[str | None, str, str]] = []
    for raw_tokens in segments:
        tokens = _strip_invocation_prefix(raw_tokens)
        if not tokens:
            continue
        if os.path.basename(tokens[0]) != "gh":
            continue
        parsed = (_parse_issue_close(tokens) or _parse_pr_merge(tokens)
                  or _parse_api_close(tokens))
        if parsed:
            targets.append(parsed)
    return targets


def resolve_repo(explicit: str | None, cwd: str | None = None) -> str | None:
    if explicit:
        return explicit
    out = _gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner",
              cwd=cwd)
    return out.strip() if out and out.strip() else None


def labels_for(repo: str, issue: str) -> set[str] | None:
    """Label names on the issue, or None when they cannot be read.

    `--jq` emits string scalars raw (like `jq -r`), one label name per line
    -- verified empirically, and the same assumption
    `block_data_migration_close.py:255-258` makes.
    """
    out = _gh("api", f"repos/{repo}/issues/{issue}", "--jq", ".labels[].name")
    if out is None:
        return None
    return {line.strip() for line in out.splitlines() if line.strip()}


#: `feat/1476-...`, `fix/1429-...`, `docs/1367-...`, `spike/1434-...` -- the
#: branch naming this project actually uses. Verified across the 30 most
#: recent merged PRs: every one carries its issue number here.
BRANCH_ISSUE = re.compile(r"^[a-z]+/(\d+)[-/]")


def issues_closed_by_pr(repo: str, pr: str) -> list[str] | None:
    """Issue numbers this PR is for, resolved from its BRANCH NAME.

    A PR carries no labels of its own that mean anything here -- the gate
    lives on the issue -- so a `gh pr merge` has to be mapped back to one.

    Not `closingIssuesReferences`: that edge is populated only by GitHub's
    auto-close keywords ("Closes #N"), and `block_lane1_status_claims.py`
    **blocks that syntax outright** in this project, so the edge is
    structurally always empty here. Measured live across the 30 most recent
    merged hrse PRs: `closes=0` on every single one, while the branch name
    carried the issue number on every single one. Resolving through the
    GraphQL edge produced a gate that could never fire even once its query
    was well-formed.
    """
    out = _gh("pr", "view", pr, "--repo", repo, "--json", "headRefName",
              "--jq", ".headRefName")
    if out is None:
        return None
    match = BRANCH_ISSUE.match(out.strip())
    return [match.group(1)] if match else []


def _deny_message(repo: str, issue: str, via_pr: str | None) -> str:
    what = (f"PR #{via_pr}, which is for {repo}#{issue}," if via_pr
            else f"{repo}#{issue}")
    return (
        f"Blocked: {what} is labelled {TOOLING_EXCEPTION_LABEL!r} and does "
        f"not carry {PRECLOSE_LABEL!r}, so nothing records that "
        f"preclose-inspection ran on the diff (hrse#1487).\n\n"
        f"hrse#1476 merged exactly this way on 2026-09-01: implemented, "
        f"verified, pushed, and headed straight to merge/close, skipping the "
        f"review CLAUDE.md requires. Re-run for real it found 5 defects, one "
        f"of which would have shipped the feature inert and green forever.\n\n"
        f"Run preclose-inspection on the diff, act on its findings, post them "
        f"as a comment, then:\n"
        f"  gh issue edit {issue} --repo {repo} --add-label {PRECLOSE_LABEL}\n\n"
        f"This hook only makes the merge/close require a deliberate labelling "
        f"action -- it cannot prevent one, and does not see graphql "
        f"mutations, heredoc bodies, or web-UI merges and closes."
    )


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

    targets = find_gated_targets(command)
    if not targets:
        _allow()
        return

    resolved: dict[str | None, str | None] = {}
    for explicit_repo, number, kind in targets:
        if explicit_repo not in resolved:
            resolved[explicit_repo] = resolve_repo(explicit_repo, payload_cwd)
        repo = resolved[explicit_repo]
        if repo is None:
            continue

        if kind == "pr":
            issues = issues_closed_by_pr(repo, number)
            if not issues:  # unlinked PR, or unreadable -- fail open
                continue
            pairs = [(issue, number) for issue in issues]
        else:
            pairs = [(number, None)]

        for issue, via_pr in pairs:
            labels = labels_for(repo, issue)
            if labels is None:  # fail-open, same rationale as _gh
                continue
            if TOOLING_EXCEPTION_LABEL not in labels:
                continue  # opt-in: not scoped to the Tooling Exception
            if PRECLOSE_LABEL in labels:
                continue

            _deny(_deny_message(repo, issue, via_pr))
            return

    _allow()


if __name__ == "__main__":
    main()
