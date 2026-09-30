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
from datetime import datetime
from pathlib import Path

from hook_identity import repo_from_checkout as _repo_from_checkout, run_gh as _run_gh  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

from shell_parse import command_segments, strip_invocation_prefix  # noqa: E402

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



def _batch_note(message: str, target_key: str | None = None,
                now: datetime | None = None) -> str:
    """Append the interrupted-batch line (harmonic-forge#509 AC3).

    Message-only. Never softens this hook's verdict — see `batch_context`'s
    docstring for why a hook consulting `batch_auth` to return `allow` would
    reintroduce the `#336` composition failure.
    """
    try:
        from batch_context import annotate  # noqa: PLC0415

        return annotate(message, target_key=target_key, now=now)
    except Exception:
        return message

def _deny(reason: str, target_key: str | None = None) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _batch_note(reason, target_key),
        },
        "systemMessage": _batch_note(reason, target_key),
    }))


def _gh(*args: str, cwd: str | None = None, repo: str | None = None) -> str | None:
    """Return stdout, or None on any failure. Fail-open, see module docstring."""
    if shutil.which("gh") is None:
        return None
    try:
        result = _run_gh(args, repo=repo, cwd=cwd, timeout=7)
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


def _parse_api_merge(tokens: list[str]) -> tuple[str, str, str] | None:
    """`gh api ... -X PUT .../pulls/N/merge` -> (repo, pr_number, kind='pr').

    harmonic-forge#549 AC4. This hook parsed exactly three shapes -- `gh issue
    close`, `gh pr merge`, and `gh api PATCH ... state=closed` -- so the REST
    merge form matched none of them and passed unexamined. `batch_auth` has
    always handled both merge forms (`classify_pr_merge` checks
    `API_MERGE_PATH` plus `_method_is(rest, "PUT")`), which is why BATCH
    stopped a live `gh api -X PUT` merge on 2026-09-09 while this guard did
    not look at it at all. Two hooks parsing the same command class to
    different depths is the gap.

    `API_MERGE_PATH` is imported from `batch_auth` rather than re-declared: a
    second copy of the pattern is what drifts, and the two hooks disagreeing
    about what counts as a merge is the defect being fixed.
    """
    if len(tokens) < 3 or tokens[1] != "api":
        return None

    from batch_auth import API_MERGE_PATH  # noqa: PLC0415

    joined = tokens[2:]
    is_put = False
    target: tuple[str, str] | None = None

    for i, token in enumerate(joined):
        upper = token.upper()
        if upper in ("-XPUT", "--METHOD=PUT", "-X=PUT"):
            is_put = True
        if token in ("-X", "--method") and i + 1 < len(joined):
            if joined[i + 1].upper() == "PUT":
                is_put = True
        match = API_MERGE_PATH.search(token)
        if match:
            target = (match.group(1), match.group(2))

    if is_put and target:
        return (target[0], target[1], "pr")
    return None


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
        shell_index = next((i for i, token in enumerate(raw_tokens)
                            if Path(token).name in {"bash", "sh", "zsh"}
                            and i + 2 < len(raw_tokens)
                            and raw_tokens[i + 1] in {"-c", "-lc", "--command"}), None)
        if shell_index is not None:
            nested_targets = find_gated_targets(raw_tokens[shell_index + 2])
            if nested_targets is None:
                return None
            targets.extend(nested_targets)
            continue
        tokens = strip_invocation_prefix(raw_tokens)
        if not tokens:
            continue
        if os.path.basename(tokens[0]) != "gh":
            continue
        parsed = (_parse_issue_close(tokens) or _parse_pr_merge(tokens)
                  or _parse_api_merge(tokens) or _parse_api_close(tokens))
        if parsed:
            targets.append(parsed)
    return targets


def resolve_repo(explicit: str | None, cwd: str | None = None) -> str | None:
    if explicit:
        return explicit
    known = _repo_from_checkout(cwd)  # harmonic-forge#804: before any gh call
    if known:
        return known
    out = _gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner",
              cwd=cwd)
    return out.strip() if out and out.strip() else None


def labels_for(repo: str, issue: str) -> set[str] | None:
    """Label names on the issue, or None when they cannot be read.

    `--jq` emits string scalars raw (like `jq -r`), one label name per line
    -- verified empirically, and the same assumption
    `block_data_migration_close.py:255-258` makes.
    """
    out = _gh("api", f"repos/{repo}/issues/{issue}", "--jq", ".labels[].name", repo=repo)
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
              "--jq", ".headRefName", repo=repo)
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


def _pr_head_sha(repo: str, pr: str) -> str | None:
    out = _gh("pr", "view", pr, "--repo", repo, "--json", "headRefOid",
              "--jq", ".headRefOid", repo=repo)
    return out.strip() if out and out.strip() else None


def _receipt(repo: str, issue: str) -> dict | None:
    """The issue's preclose receipt, or None on any failure (fail closed)."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gh"))
        from preclose_check import find_receipt  # noqa: PLC0415
        return find_receipt(repo, int(issue))
    except Exception:
        return None


def _pr_patch_id(repo: str, pr: str | None) -> str | None:
    """harmonic-forge#834 AC3: `git patch-id --stable` of the PR's diff, from
    `gh pr diff`, so no local checkout of the PR is needed. None on failure."""
    if not pr:
        return None
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gh"))
        import preclose_passes  # noqa: PLC0415
    except Exception:
        return None
    return preclose_passes.patch_id(_gh("pr", "diff", pr, "--repo", repo, repo=repo))


def _preclose_receipt_ok(repo: str, issue: str, head_sha: str, via_pr: str | None = None) -> bool:
    """harmonic-forge#778 AC3. `True` only when a `status: complete`
    receipt for `repo`#`issue` names `head_sha` as its `reviewed_sha`, or
    (harmonic-forge#834 AC3) its `reviewed_patch_id` equals the PR's current
    patch-id: a rebase that leaves the diff unchanged is not a new pass.

    Fails CLOSED (returns `False`, which denies) on any resolution failure
    -- unlike the rest of this hook's fail-open style. Everywhere else, a
    read failure means "cannot tell if GitHub state warrants a block," and
    the honest answer is "don't block on a guess." Here, "cannot read the
    receipt" and "no reviewed diff is on file for this SHA" are the SAME
    fact, and that fact is exactly what AC3 exists to deny on -- fail-open
    here would let the label alone (already known stale-able, per the
    issue's Cause 3) stand in for a receipt that binds to nothing.
    """
    # harmonic-forge#834 preclose: read the last COMPLETED pass from the
    # receipt's history, not the receipt's own status -- an abandoned `--plan`
    # for the next pass rewrites the status to "planned" and must not
    # un-review the diff the last pass covered.
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gh"))
        import preclose_passes  # noqa: PLC0415
        last = preclose_passes.reviewed(_receipt(repo, issue))
    except Exception:
        return False
    if not last:
        return False
    if last.get("sha") == head_sha:
        return True
    reviewed_patch = last.get("patch_id")
    return bool(reviewed_patch) and reviewed_patch == _pr_patch_id(repo, via_pr)


def _stale_receipt_message(repo: str, issue: str, via_pr: str, head_sha: str) -> str:
    # harmonic-forge#834 AC4: at the cap, never tell the session to run
    # another pass -- that instruction is what produced #829's third pass.
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gh"))
        import preclose_passes  # noqa: PLC0415
        passes = preclose_passes.current(preclose_passes.history(_receipt(repo, issue)))
        if len(passes) >= preclose_passes.MAX_PASSES:
            return (f"Blocked: PR #{via_pr}, which is for {repo}#{issue}, has no completed "
                    f"pre-close receipt covering its current head {head_sha[:12]} "
                    f"(harmonic-forge#778 AC3).\n\n{preclose_passes.cap_message(passes)}")
    except Exception:
        pass
    return (
        f"Blocked: PR #{via_pr}, which is for {repo}#{issue}, carries "
        f"{PRECLOSE_LABEL!r} but no completed pre-close receipt names its "
        f"current head {head_sha[:12]} as reviewed (harmonic-forge#778 "
        f"AC3). The label says a review happened once; it says nothing "
        f"about which diff -- new commits can land after the label was "
        f"added, exactly as they did on harmonic-forge#774.\n\n"
        f"Run the pre-close pass against this head and record it:\n"
        f"  python3 ~/harmonic-forge/tools/gh/preclose_check.py "
        f"--repo {repo} --issue {issue} --head {head_sha} --complete ...\n\n"
        f"See preclose_check.py --help for the full plan/complete flow."
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

            # harmonic-forge#509: pass the acting key so the annotation can
            # name THIS issue rather than listing the batch. "stopped on F509"
            # is actionable; "a batch is live" is not — and with more live keys
            # than the elision cap, the acting key may not even appear in the
            # list.
            try:
                from batch_auth import issue_key  # noqa: PLC0415

                _acting = issue_key(repo, issue)
            except Exception:
                _acting = None

            if PRECLOSE_LABEL not in labels:
                _deny(_deny_message(repo, issue, via_pr), target_key=_acting)
                return

            # harmonic-forge#778 AC3/AC5: the label alone says a review
            # happened once, never which diff. Binding to the head SHA is
            # meaningful only for a merge (a PR has a head that can move
            # after labelling) -- a bare `gh issue close` has no SHA to bind
            # to, so it stays label-only, exactly as before.
            if kind == "pr":
                head_sha = _pr_head_sha(repo, number)
                # Preclose finding: this is NOT the same "cannot read GitHub"
                # case the fail-open rationale elsewhere in this file covers.
                # By this point the issue is confirmed tooling-exception AND
                # preclose-inspected labelled -- a SHA-bound receipt is
                # required, and "cannot determine the head SHA" and "the
                # receipt doesn't match" are the same fact for this purpose:
                # nothing proves this diff was reviewed. Fail CLOSED, per the
                # same fail-closed precedent the receipt-match check below
                # already follows.
                if head_sha is None:
                    _deny(
                        f"Blocked: PR #{via_pr}, which is for {repo}#{issue}, carries "
                        f"{PRECLOSE_LABEL!r}, but this PR's current head SHA could not "
                        f"be determined (harmonic-forge#778 AC3). A SHA-bound receipt is "
                        f"required once an issue is labelled -- cannot verify and not "
                        f"reviewed are the same fact here. Re-run this once GitHub is "
                        f"reachable.",
                        target_key=_acting)
                    return
                if not _preclose_receipt_ok(repo, issue, head_sha, via_pr):
                    _deny(_stale_receipt_message(repo, issue, via_pr, head_sha),
                          target_key=_acting)
                    return

    _allow()


if __name__ == "__main__":
    main()
