#!/usr/bin/env python3
"""Deny raw GitHub issue-post transports for Lane 2; use `l2_post.py`.

Mirrors `block_lane1_status_claims.py`'s executed-command-shape
recognition (`is_direct_transport`, reused directly rather than
re-implemented -- a second copy is exactly the drift risk
`shell_parse.py`'s own extraction was written to avoid) and its
`LANE`-conditional gating, but flipped: that hook explicitly carves out
Lane 2/3 as free to post directly (harmonic-forge#190). This hook closes
that carve-out for Lane 2 specifically, after a live incident
(harmonic-forge#371) in which a Lane 2 session's own unverified status
claim -- and, worse, a fabricated technical justification for a
discrepancy that did not need explaining -- went out with nothing to
catch it.

**No repo-name conditional anywhere.** `is_direct_transport()` carries
two repo-specific branches (`mise run post-comment`, `post_comment.py`)
that only fire for `vitalharmony/hrse` -- both are legacy Lane 1 tooling
this hook does not need to special-case, since Lane 2 has no reason to
invoke either. The `gh issue comment/create/edit` and `gh api
.../issues/.../comments` checks this hook actually relies on are already
repo-agnostic in the shared function. This guard applies identically to
every repo, on purpose: this issue's own motivating incident happened in
the one repo a repo-scoped guard would have left unprotected.

Only `LANE == "2"` is gated. `LANE` unset or any other value is out of
scope for this hook -- Lane 1's own guard already covers `LANE`
unset/`"1"`, and Lane 3 has no raw-post restriction by design (it
reports gate results directly).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from block_lane1_status_claims import command_segments, is_direct_transport  # noqa: E402


def _is_issue_filing_command(segment: list[str]) -> bool:
    """harmonic-forge#388: Lane 2 never files (`feedback_lane2_never_creates_issues`).
    `is_direct_transport()` already catches raw `gh issue create`, but not
    the *sanctioned* Lane 1 filing path -- `gh_issue.py` / `mise run
    gh-new-issue` -- which is exactly the hole a Lane 2 session could file
    through undetected. Kept local to this hook rather than added to the
    shared `is_direct_transport()`: that function also gates Lane 1's own
    raw-post denial, where `gh_issue.py`/`gh-new-issue` is Lane 1's
    legitimate, sanctioned tool and must not be denied there."""
    if len(segment) >= 3 and segment[:3] == ["mise", "run", "gh-new-issue"]:
        return True
    if segment and Path(segment[0]).name == "gh_issue.py":
        return True
    if len(segment) >= 2 and segment[0].startswith("python") and Path(segment[1]).name == "gh_issue.py":
        return True
    return False


def _mise_task_and_rest(segment: list[str]) -> tuple[str | None, list[str]]:
    """(task_name, remaining_args) for a `mise` invocation, or (None, [])
    if `segment` isn't one. `mise <task> [args]`, `mise run <task>
    [args]`, and `mise r <task> [args]` ("r" is mise's own documented
    alias for "run") are all equally valid and equally reach the task --
    a check that only matched the `run` spelling missed the other two
    (preclose-inspection finding, live-reproduced: `mise restart --push`
    and `mise r restart --push` both bypassed the original check)."""
    if len(segment) < 2 or segment[0] != "mise":
        return None, []
    if segment[1] in ("run", "r") and len(segment) >= 3:
        return segment[2], segment[3:]
    return segment[1], segment[2:]


def _is_push_or_pr_create(segment: list[str]) -> bool:
    """harmonic-forge#398: push/PR is categorically Lane 1's
    (`feedback_lane2_never_pushes_or_prs`) -- Lane 2 stops at a committed
    branch in its worktree and reports; Lane 1 pushes and opens the PR.
    Recognizes the raw forms, `git`'s own `-C <dir>` global flag (matching
    `block_irreversible_ops.py`'s established pattern for the same class
    of gap -- a global flag before the subcommand slipping past a
    positional check), and sanctioned-wrapper equivalents a Lane 2
    session could otherwise reach for (preclose-inspection finding,
    live-reproduced across two rounds -- the sibling
    `_is_issue_filing_command()` above already recognizes its own
    wrapper equivalent, this one initially did not):

    - `gh-as <account> <command...>` (`rules/universal-agent.md`) scopes
      `gh` to a named account for one command -- strip the wrapper and
      recurse on what it wraps, so `gh-as vitalharmony gh pr create ...`
      is caught the same as the bare form.
    - `mise (run|r)? commit --push` / `mise (run|r)? restart --push`
      (HRSE2's own documented push path, `CLAUDE.md`: "Push to GitHub
      ... only when explicitly requested") forward to
      `scripts/git_commit.py`, which runs `git push` internally -- the
      push never appears as a literal `git push` token in the executed
      command, only as this flag. All three mise invocation spellings
      are recognized via `_mise_task_and_rest()`, not just `mise run`.
    - `scripts/git_commit.py --push` invoked directly (bare, `python3`-
      prefixed, or any path) -- the actual tool that runs `git push`
      internally, independent of which mise spelling (or none) reached
      it. Mirrors `_is_issue_filing_command()`'s own direct-script
      recognition for `gh_issue.py`."""
    if segment and Path(segment[0]).name == "gh-as" and len(segment) >= 3:
        return _is_push_or_pr_create(segment[2:])
    if not segment or segment[0] != "git":
        args = segment
    else:
        args = segment[1:]
        while len(args) >= 2 and args[0] in ("-C", "--git-dir", "--work-tree"):
            args = args[2:]
        args = ["git", *args]
    if len(args) >= 2 and args[0] == "git" and args[1] == "push":
        return True
    if len(args) >= 3 and args[0] == "gh" and args[1] == "pr" and args[2] == "create":
        return True
    task, rest = _mise_task_and_rest(segment)
    if task in ("commit", "restart") and any(a == "--push" or a.startswith("--push=") for a in rest):
        return True
    is_git_commit_script = (
        segment
        and (
            Path(segment[0]).name == "git_commit.py"
            or (len(segment) >= 2 and segment[0].startswith("python") and Path(segment[1]).name == "git_commit.py")
        )
    )
    if is_git_commit_script and any(a == "--push" or a.startswith("--push=") for a in segment[1:]):
        return True
    return False


def raw_post_denial(command: str, cwd: Path) -> str | None:
    if os.environ.get("LANE") != "2":
        return None
    effective_cwd = cwd
    for segment in command_segments(command):
        if len(segment) == 2 and segment[0] == "cd":
            target = Path(segment[1]).expanduser()
            effective_cwd = target if target.is_absolute() else effective_cwd / target
            continue
        if is_direct_transport(segment, effective_cwd, prefer_cwd=True):
            return (
                "raw GitHub issue posting bypasses Lane 2's receipt-backed "
                "status wrapper (harmonic-forge#371). Use `python3 "
                "tools/gh/l2_post.py post --kind plan|completion|blocked ...` "
                "(or the sanctioned `mise run l2-post` task) so the status is "
                "composed from verified receipts and self-checked by "
                "post/fetch/diff, not asserted."
            )
        if _is_issue_filing_command(segment):
            return (
                "Lane 2 never creates a GitHub issue, even via the sanctioned "
                "filing path -- issue creation is categorically Lane 1's job "
                "(harmonic-forge#388). Surface the finding in your completion "
                "report and stop; Lane 1 files it."
            )
        if _is_push_or_pr_create(segment):
            return (
                "push and PR creation are categorically Lane 1's "
                "(harmonic-forge#398, feedback_lane2_never_pushes_or_prs). "
                "Stop at the committed branch in your worktree and report "
                "in your completion comment; Lane 1 pushes and opens the PR."
            )
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
    if not isinstance(command, str):
        return denial("Blocked: malformed Bash hook payload; refusing to bypass Lane 2 posting controls.")
    try:
        reason = raw_post_denial(command, cwd)
    except (AttributeError, TypeError, ValueError):
        return denial("Blocked: malformed shell command; refusing to bypass Lane 2 posting controls.")
    if reason is not None:
        return denial(f"Blocked: {reason}")
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
