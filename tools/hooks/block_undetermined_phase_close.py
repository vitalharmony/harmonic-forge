#!/usr/bin/env python3
"""PreToolUse hook: a `phase`/`epic` issue does not close without a
`shipped`/`shipped-inert` determination (harmonic-forge#642 slice 1).

## Root cause

hrse#195 closed 2026-09-01T05:02:43Z by a manual close (GraphQL
`ClosedEvent.closer` = null), carrying only the label `feature`. Every
close gate that existed keyed on a label naming what KIND of issue it is
(`block_data_migration_close.py` on `data-migration`,
`block_missing_preclose_inspection.py` on `tooling-exception`). Nothing
marked "other work is specified against this" (a phase or epic), and
nothing asked whether the work produces output on real data. Nothing
could refuse hrse#195's close, so a correct-but-inert implementation
closed and was read downstream as live.

## What this guarantees, stated exactly

Modeled on `block_missing_preclose_inspection.py`: same `_allow`/`_deny`
shape, same fail-open `_gh` helper with a 7s timeout, same
`shell_parse.command_segments` segmentation, same `_batch_note`
annotation.

Opt-in on the `phase` or `epic` label. `gh issue close`, `gh api PATCH
... state=closed`, `gh pr merge`, and `gh api PUT .../merge` are blocked
on a `phase`/`epic` issue unless it carries exactly one of `shipped` /
`shipped-inert`, with the evidence each requires (see `_decide` below).

**Exemption:** a close with reason `not planned` or `duplicate` (CLI
`-r`/`--reason`, `--duplicate-of`, or REST `state_reason=not_planned` /
`state_reason=duplicate`) skips the gate entirely -- a descoped phase
produces nothing by design and needs no determination. Checked before
any network call.

**PR merge path:** resolved only through a closing-keyword reference (in
the PR body, its commit messages, or the merge command's own
`--body`/`-b`/`--subject` values) to a `phase`/`epic` issue -- NOT
through branch-name resolution the way the sibling hook does, because
this control is specifically about the keyword auto-close path
(`hrse#1811`/PR #1813, `harmonic-forge#640`/PR #641 are both live
examples under the BATCH exception, harmonic-forge#612).

**It does not and cannot prevent the merge or close.** Fail-open by
design, same rationale as every sibling hook: it sees nothing of `gh api
graphql` mutations, heredoc bodies, the GitHub web UI, or a merge/close
issued from Python or curl. It checks that the evidence artifact
EXISTS and comes after the label -- never that the output is genuinely
from real data. Only a human reviewer can check that. The `phase`/`epic`
label itself has to be applied by a human at filing time; nothing here
enforces that either -- the after-the-fact sweep
(`repo_hygiene.audit_phase_closures`) is the backstop for both gaps, the
same relationship `data-migration` has to `block_data_migration_close.py`
and `repo_hygiene.audit_migrations` (hrse#867/#871).

Its own CI backstop is currently shipped-inert: HRSE2's weekly hygiene
workflow lacks a token able to read PRs (hrse#842), so a close that slips
past this hook today is caught only by someone running
`python3 tools/gh/repo_hygiene.py --repo <repo>` by hand.
"""
from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from shell_parse import command_segments, strip_invocation_prefix  # noqa: E402

DETERMINATION_LABELS = ("shipped", "shipped-inert")
GATED_LABELS = ("phase", "epic")

#: Byte-identical to `tools/gh/block_closing_keywords.py`'s `CLOSING_KEYWORD`.
#: Parity is pinned in `test_block_undetermined_phase_close.py` by importing
#: both and comparing patterns -- this hook does not import `tools/gh` at
#: runtime (a hooks module must not depend on the `tools/gh` package).
CLOSING_KEYWORD = re.compile(
    r"(?i)\b(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)"
    r"\s+(?P<repo>[\w.-]+/[\w.-]+)?#(?P<number>\d+)"
)

#: The URL form of the same keywords -- "Closes https://github.com/o/r/issues/N".
CLOSING_KEYWORD_URL = re.compile(
    r"(?i)\b(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)"
    r"\s+https://github\.com/(?P<repo>[\w.-]+/[\w.-]+)/issues/(?P<number>\d+)"
)

FENCE = re.compile(r"^\s*(?:```|~~~)")

REASON_FLAGS = ("-r", "--reason")


def _allow() -> None:
    print(json.dumps({}))


def _batch_note(message: str, target_key: str | None = None) -> str:
    """Append the interrupted-batch line (harmonic-forge#509 AC3)."""
    try:
        from batch_context import annotate  # noqa: PLC0415

        return annotate(message, target_key=target_key)
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


def _gh_json(*args: str, cwd: str | None = None):
    out = _gh(*args, cwd=cwd)
    if out is None:
        return None
    try:
        return json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return None


def resolve_repo(explicit: str | None, cwd: str | None = None) -> str | None:
    if explicit:
        return explicit
    out = _gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner",
              cwd=cwd)
    return out.strip() if out and out.strip() else None


def _flag_value(tokens: list[str], names: tuple[str, ...]) -> str | None:
    for i, token in enumerate(tokens):
        for name in names:
            if token == name and i + 1 < len(tokens):
                return tokens[i + 1]
            if token.startswith(f"{name}="):
                return token.split("=", 1)[1]
    return None


def _has_field(tokens: list[str], assignment: str) -> bool:
    return any(token.replace(" ", "") == assignment for token in tokens)


def is_exempt_close(tokens: list[str]) -> bool:
    """`--duplicate-of`, `-r`/`--reason not planned|duplicate` (CLI), or
    `state_reason=not_planned|duplicate` (REST). Checked before any `gh` call.
    """
    if any(t == "--duplicate-of" or t.startswith("--duplicate-of=") for t in tokens):
        return True
    reason = _flag_value(tokens, REASON_FLAGS)
    if reason and reason.strip().strip("'\"").lower() in ("not planned", "duplicate"):
        return True
    if _has_field(tokens, "state_reason=not_planned") or _has_field(tokens, "state_reason=duplicate"):
        return True
    return False


ISSUE_NUMBER = re.compile(r"^#?(\d+)$")


_ISSUE_URL = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)")
_PR_URL = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")


def _url_target(tokens: list[str], pattern: re.Pattern[str]) -> tuple[str, str] | None:
    """URL-form (repo, number) fallback, the way the sibling hook's own
    `parse` does (`block_missing_preclose_inspection.py`'s
    `_parse_positional_target`)."""
    for token in tokens:
        match = pattern.search(token)
        if match:
            return match.group(1), match.group(2)
    return None


def _bare_number(tokens: list[str]) -> str | None:
    for token in tokens:
        if not token.startswith("-") and ISSUE_NUMBER.match(token):
            return ISSUE_NUMBER.match(token).group(1)
    return None


def classify_issue_close_target(tokens: list[str]) -> tuple[str | None, str | None] | None:
    """Reuses `batch_auth.classify_issue_close`; URL-form fallback if it
    resolves no number (binding pitch-inspection change #2)."""
    from batch_auth import classify_issue_close  # noqa: PLC0415

    parsed = classify_issue_close(tokens)
    if parsed is None:
        return None
    repo, number = parsed
    if number is None:
        url = _url_target(tokens, _ISSUE_URL)
        if url:
            return url
        number = _bare_number(tokens)
    return (repo, number) if number else None


def classify_pr_merge_target(tokens: list[str]) -> tuple[str | None, str | None] | None:
    from batch_auth import classify_pr_merge  # noqa: PLC0415

    parsed = classify_pr_merge(tokens)
    if parsed is None:
        return None
    repo, number = parsed
    if number is None:
        url = _url_target(tokens, _PR_URL)
        if url:
            return url
        bare = _bare_number(tokens)
        return (repo, bare) if bare else None
    return (repo, str(number))


BODY_FLAGS = ("--body", "-b", "--subject", "-t", "--title")


def _closing_keyword_targets(text: str, this_repo: str) -> set[str]:
    targets: set[str] = set()
    for match in CLOSING_KEYWORD.finditer(text):
        repo = match.group("repo") or this_repo
        if repo == this_repo:
            targets.add(match.group("number"))
    for match in CLOSING_KEYWORD_URL.finditer(text):
        if match.group("repo") == this_repo:
            targets.add(match.group("number"))
    return targets


def issues_closed_by_pr(repo: str, pr: str, merge_tokens: list[str],
                         cwd: str | None) -> set[str]:
    """Every issue this PR merge would close via a closing keyword --
    scanned in the PR body, its commit messages, and the merge command's
    own `--body`/`-b`/`--subject` values (pitch-inspection binding change
    #1). Unreadable body or no keyword found -> empty set (fail open).
    """
    targets: set[str] = set()

    for value in (_flag_value(merge_tokens[2:], (f,)) for f in BODY_FLAGS):
        if value:
            targets |= _closing_keyword_targets(value, repo)

    body = _gh("api", f"repos/{repo}/pulls/{pr}", "--jq", ".body", cwd=cwd)
    if body:
        targets |= _closing_keyword_targets(body, repo)

    commits = _gh("api", f"repos/{repo}/pulls/{pr}/commits",
                   "--jq", ".[].commit.message", cwd=cwd)
    if commits:
        targets |= _closing_keyword_targets(commits, repo)

    return targets


def labels_for(repo: str, issue: str) -> set[str] | None:
    out = _gh("api", f"repos/{repo}/issues/{issue}", "--jq", ".labels[].name")
    if out is None:
        return None
    return {line.strip() for line in out.splitlines() if line.strip()}


def _latest_label_event_time(repo: str, issue: str, label: str) -> str | None:
    events = _gh_json("api", f"repos/{repo}/issues/{issue}/events",
                       "--paginate")
    if events is None:
        return None
    latest: str | None = None
    for event in events:
        if event.get("event") == "labeled" and (event.get("label") or {}).get("name") == label:
            created = event.get("created_at")
            if created and (latest is None or created > latest):
                latest = created
    return latest


def _has_evidence_comment(repo: str, issue: str, since: str) -> bool | None:
    comments = _gh_json("api", f"repos/{repo}/issues/{issue}/comments",
                         "--paginate")
    if comments is None:
        return None
    for comment in comments:
        created = comment.get("created_at") or ""
        if created < since:
            continue
        body = comment.get("body") or ""
        lines = body.splitlines()
        fences = sum(1 for line in lines if FENCE.match(line))
        if fences >= 2:
            return True
    return False


def _has_open_blocker(repo: str, issue: str) -> bool | None:
    items = _gh_json("api", f"repos/{repo}/issues/{issue}/dependencies/blocked_by")
    if items is None:
        return None
    return any(item.get("state") == "open" for item in items)


def _next_commands(repo: str, issue: str) -> str:
    return (
        f"  gh issue edit {issue} --repo {repo} --add-label shipped|shipped-inert\n"
        f"  # evidence for shipped: a comment posted after the shipped label, "
        f"containing a fenced code block with real-data output\n"
        f"  # blocker for shipped-inert: "
        f"gh api repos/{repo}/issues/{issue}/dependencies/blocked_by "
        f"-f issue_id=<supply-issue-node-id> -X POST"
    )


def _deny_message(repo: str, issue: str, via_pr: str | None, condition: str) -> str:
    what = (f"PR #{via_pr}, which closes {repo}#{issue} by keyword," if via_pr
            else f"{repo}#{issue}")
    return (
        f"Blocked: {what} carries `phase`/`epic` -- other work may be "
        f"specified against it -- and {condition} (harmonic-forge#642). "
        f"hrse#195 closed exactly this way: correct, verified, ten of ten "
        f"tests passing, and it produces nothing. Three downstream phases "
        f"assumed it was live.\n\n"
        f"{_next_commands(repo, issue)}\n\n"
        f"gh issue close --comment posts the comment AFTER this check runs, "
        f"so it cannot satisfy the evidence requirement.\n"
        f"For the PR path: remove the closing keyword with `gh pr edit`, "
        f"merge, then determine and close the issue separately.\n\n"
        f"Limits, stated exactly: fail-open by design; does not see GraphQL "
        f"mutations, heredoc bodies, web-UI closes, or non-Bash closes; "
        f"checks that the evidence artifact exists, not that it is true."
    )


def _decide(repo: str, issue: str) -> str | None:
    """None = allow. A reason string = deny."""
    labels = labels_for(repo, issue)
    if labels is None:
        return None  # fail open
    if not (set(labels) & set(GATED_LABELS)):
        return None

    shipped = "shipped" in labels
    inert = "shipped-inert" in labels

    if not shipped and not inert:
        return "carries neither `shipped` nor `shipped-inert`"
    if shipped and inert:
        return "carries BOTH `shipped` and `shipped-inert` -- exactly one is required"

    if shipped:
        since = _latest_label_event_time(repo, issue, "shipped")
        if since is None:
            return None  # fail open -- events unreadable
        has_evidence = _has_evidence_comment(repo, issue, since)
        if has_evidence is None:
            return None  # fail open
        if not has_evidence:
            return ("is labelled `shipped` but no comment posted after that "
                    "label contains a fenced block of real-data output")
        return None

    # inert
    has_blocker = _has_open_blocker(repo, issue)
    if has_blocker is None:
        return None  # fail open
    if not has_blocker:
        return ("is labelled `shipped-inert` but has no OPEN `blocked_by` "
                "supply issue")
    return None


def _batch_key(repo: str, issue: str) -> str | None:
    try:
        from batch_auth import issue_key  # noqa: PLC0415

        return issue_key(repo, issue)
    except Exception:
        return None


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

    try:
        segments = command_segments(command)
    except ValueError:
        _allow()
        return

    from batch_auth import _protected_graphql  # noqa: PLC0415

    resolved: dict[str | None, str | None] = {}

    for raw_tokens in segments:
        tokens = strip_invocation_prefix(raw_tokens)
        if not tokens or Path(tokens[0]).name != "gh":
            continue
        if _protected_graphql(raw_tokens):
            # batch_gate already asks on these; this hook stays silent.
            continue

        rest = tokens[1:]

        if is_exempt_close(tokens):
            continue

        targets: list[tuple[str | None, str, str | None]] = []  # (repo, issue, via_pr)

        issue_target = classify_issue_close_target(tokens)
        if issue_target and issue_target[1]:
            targets.append((issue_target[0], issue_target[1], None))

        pr_target = classify_pr_merge_target(tokens)
        if pr_target and pr_target[1]:
            explicit_repo = pr_target[0]
            if explicit_repo not in resolved:
                resolved[explicit_repo] = resolve_repo(explicit_repo, payload_cwd)
            repo = resolved[explicit_repo]
            if repo is None:
                continue
            issues = issues_closed_by_pr(repo, pr_target[1], rest, payload_cwd)
            for issue in issues:
                targets.append((explicit_repo, issue, pr_target[1]))

        for explicit_repo, issue, via_pr in targets:
            if explicit_repo not in resolved:
                resolved[explicit_repo] = resolve_repo(explicit_repo, payload_cwd)
            repo = resolved[explicit_repo]
            if repo is None:
                continue

            condition = _decide(repo, issue)
            if condition is None:
                continue

            _deny(_deny_message(repo, issue, via_pr, condition),
                  target_key=_batch_key(repo, issue))
            return

    _allow()


if __name__ == "__main__":
    main()
