#!/usr/bin/env python3
"""PostToolUse Bash: a merge, a PR or a close owes a task-list update (harmonic-forge#1126 AC2).

The lane post scripts record their own markers (`tasklist_owed.record_post`),
because they know the issue and the kind exactly. This hook covers the three
`gh` commands no script of ours wraps: `gh pr merge`, `gh pr create` and
`gh issue close`, parsed with `batch_auth`'s classifiers over
`shell_parse.command_segments`. Every recognized command records (reforge
epoch 3): there is no check that it landed and no reading of its output, so
a failed one costs one entry update. Every failure is silent. The entry shape is in
`tasklist_hold.py`.
"""
from __future__ import annotations

import json
import os
import re
import sys
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
#: a merge's PR-carrier read (30). Its `.claude/settings.json` timeout must
#: exceed this, or Claude Code kills the hook before any marker is written.
NETWORK_BUDGET = 15 + 30
#: This hook's timeout in `.claude/settings.json` (pinned by a test).
HOOK_TIMEOUT = 90


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

    Reforge epoch 3: every recognized command records, with no check that the
    action landed and no reading of the output. Recording too much costs one
    entry update, and Stop treats every marker alike. `on_event` is called as
    each one is found, so a hook killed at its timeout keeps every marker
    already written; a killed write is recorded again by the next post."""
    import batch_auth  # noqa: PLC0415
    command = (payload.get("tool_input") or {}).get("command") or ""
    found: list[tuple[str, str]] = []

    def add(key: str | None, kind: str) -> None:
        if key:
            found.append((key, kind))
            if on_event:
                on_event(key, kind)

    for segment in command_segments(command):
        close = batch_auth.classify_issue_close(segment)
        if close and close[1]:
            repo = close[0] or _short_repo(segment) or _cwd_repo(payload)
            if repo:
                add(batch_auth.issue_key(repo, close[1]), "close")
            continue
        merge = batch_auth.classify_pr_merge(segment)
        if merge and merge[1]:
            repo = merge[0] or _short_repo(segment) or _cwd_repo(payload)
            if repo:
                add(batch_auth.derive_issue_key(repo, merge[1], {}), "merge")
            continue
        tokens = strip_invocation_prefix(segment)
        if tokens[:3] and Path(tokens[0]).name == "gh" and tokens[1:3] == ["pr", "create"]:
            repo = batch_auth._repo_flag(tokens) or _short_repo(tokens) or _cwd_repo(payload)
            if repo:
                add(_title_key(tokens, repo), "pr")
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
