#!/usr/bin/env python3
"""PostToolUse Bash: a merge, a PR or a close owes a task-list update (harmonic-forge#1126 AC2).

The lane post scripts record their own markers (`tasklist_owed.record_post`),
because they know the issue and the kind exactly. This hook covers the three
`gh` commands no script of ours wraps: `gh pr merge`, `gh pr create` and
`gh issue close`, parsed with `batch_auth`'s classifiers over
`shell_parse.command_segments`. PostToolUse fires on failure too, so a merge or
close is recorded only when `batch_auth.action_landed` confirms it, and a PR
only when the output names one. Every failure is silent. The entry shape is in
`tasklist_hold.py`.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tasklist_owed as owed  # noqa: E402
from shell_parse import command_segments, strip_invocation_prefix  # noqa: E402

_PR_URL = re.compile(r"github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)")
_TITLE_FLAGS = ("--title", "-t")


def _output(payload: dict) -> str:
    response = payload.get("tool_response")
    if isinstance(response, dict):
        return f"{response.get('stdout') or ''}\n{response.get('stderr') or ''}"
    return str(response or "")


def _title_key(tokens: list[str], pr_repo: str) -> str | None:
    import batch_auth  # noqa: PLC0415
    title = next((tokens[i + 1] for i, t in enumerate(tokens[:-1]) if t in _TITLE_FLAGS), "")
    keys = {batch_auth._carrier_key(slug, number, pr_repo)
            for slug, number in batch_auth._TITLE_ISSUE.findall(title)}
    keys.discard(None)
    return keys.pop() if len(keys) == 1 else None


def _short_repo(tokens: list[str]) -> str | None:
    """`-R owner/repo`, which `batch_auth._repo_flag` does not read."""
    return next((tokens[i + 1] for i, t in enumerate(tokens[:-1]) if t == "-R"), None)


#: Seconds of network one Bash call can cost this hook: `gh repo view` (15) and
#: `batch_auth.action_landed` (30), plus its PR-carrier read (30) on a merge.
#: Its `.claude/settings.json` timeout must exceed this (preclose epoch 1 pass 2),
#: or Claude Code kills the hook before any marker is written.
NETWORK_BUDGET = 15 + 30 + 30
#: This hook's timeout in `.claude/settings.json` (pinned by a test).
HOOK_TIMEOUT = 90
_ALREADY_CLOSED = re.compile(r"\balready closed\b", re.I)


def _cwd_repo(payload: dict) -> str | None:
    """The repository `gh` itself would use when no repo flag is given
    (preclose pass 1: a bare `gh issue close N` recorded nothing)."""
    import subprocess  # noqa: PLC0415
    cwd = payload.get("cwd") or os.getcwd()
    try:
        done = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner", "-q",
                               ".nameWithOwner"], cwd=cwd, capture_output=True, text=True,
                              timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.strip() or None


def events(payload: dict, on_event=None) -> list[tuple[str, str]]:
    """`[(key, kind), ...]` this Bash call owes, kind in merge/pr/close.
    `on_event` is called with each one as soon as it is verified, so a hook
    killed at its timeout keeps every marker already written (epoch 2 pass 1);
    a segment needing a network read is skipped once the next one could
    outrun the hook timeout."""
    import batch_auth  # noqa: PLC0415
    command = (payload.get("tool_input") or {}).get("command") or ""
    found: list[tuple[str, str]] = []
    start = time.monotonic()

    def add(key: str, kind: str) -> None:
        found.append((key, kind))
        if on_event:
            on_event(key, kind)

    def spent() -> bool:
        return time.monotonic() - start > HOOK_TIMEOUT - NETWORK_BUDGET

    for segment in command_segments(command):
        close = batch_auth.classify_issue_close(segment)
        if close and close[1] and spent():
            continue
        if close and close[1]:
            if _ALREADY_CLOSED.search(_output(payload)):
                continue  # gh skipped it: nothing closed, nothing owed
            repo, number = close[0] or _short_repo(segment) or _cwd_repo(payload), close[1]
            if not repo:
                continue
            if batch_auth.action_landed("close", repo, number):
                key = batch_auth.issue_key(repo, number)
                if key:
                    add(key, "close")
            continue
        merge = batch_auth.classify_pr_merge(segment)
        if merge and merge[1] and spent():
            continue
        if merge and merge[1]:
            repo, number = merge[0] or _short_repo(segment) or _cwd_repo(payload), merge[1]
            if not repo:
                continue
            if batch_auth.action_landed("merge", repo, number):
                key = batch_auth.derive_issue_key(repo, number, {})
                if key:
                    add(key, "merge")
            continue
        tokens = strip_invocation_prefix(segment)
        if tokens[:3] and Path(tokens[0]).name == "gh" and tokens[1:3] == ["pr", "create"]:
            match = _PR_URL.search(_output(payload))
            if match:
                key = _title_key(tokens, match.group(1))
                if key:
                    add(key, "pr")
    return found


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(payload, dict) or not os.environ.get("LANE"):
        return 0
    if payload.get("tool_name") not in (None, "Bash"):
        return 0
    try:
        session = payload.get("session_id") or owed.session_id()
        events(payload, on_event=lambda key, kind: owed.record(key, kind, session=session))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
