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
