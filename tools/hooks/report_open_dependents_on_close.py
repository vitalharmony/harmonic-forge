#!/usr/bin/env python3
"""PostToolUse Bash: closing a blocker reports what it unblocks (harmonic-forge#1381 AC3).

A `blocked-on-prerequisite` deferral is written as a native `blocked_by` link
(`gh_issue.py --blocked-by`). Nothing watches for the blocker closing, and this
adds no watcher: the close command is the moment the edge changes, so this hook
reads `dependencies/blocking` once, for that issue, right after the close.

- Only a close that landed counts. PostToolUse fires on failure too, so
  `batch_auth.action_landed` is asked first; an unconfirmed close prints nothing.
- Each OPEN dependent becomes one "handoff now owed: <repo>#<n>" line. A closed
  dependent owes nothing.
- An unreadable `dependencies/blocking` prints "dependents UNKNOWN", never
  silence: a fail-open hook that went quiet would read as "nothing owed".
- Plain stdout from a PostToolUse hook does not reach the model; the lines go
  out as `hookSpecificOutput.additionalContext`. A hook cannot write task
  entries, so the lane writes them from this context.

`repo_hygiene.py`'s open-dependents-of-closed-blockers line is the after-the-fact
pair for a close this hook missed (killed, timed out, or run outside Claude Code).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from shell_parse import command_segments  # noqa: E402

#: One landing read plus one dependents read per close.
READ_TIMEOUT = 20
#: This hook's timeout in `.claude/settings.json`.
HOOK_TIMEOUT = 60


def _closed(payload: dict) -> list[tuple[str, str, str | None]]:
    """`[(repo, number, unknown), ...]` for each issue close in this Bash call.

    `unknown` is None for a close confirmed landed, else why it could not be
    judged. Only a close the landing read says did NOT happen is dropped
    (preclose pass 1, harmonic-forge#1381): an unresolved repo or number, an
    unreadable landing check, or an error all come back as unknown, because
    silence reads as "nothing owed"."""
    import batch_auth  # noqa: PLC0415
    import record_tasklist_post  # noqa: PLC0415
    command = (payload.get("tool_input") or {}).get("command") or ""
    found: list[tuple[str, str, str | None]] = []
    for segment in command_segments(command):
        close = batch_auth.classify_issue_close(segment)
        if not close:
            continue
        number = close[1] or "?"
        try:
            repo = record_tasklist_post._advisory_repo(segment, close[0], payload)
            if not repo or not close[1]:
                found.append((repo or "?", number, "the closed issue could not be resolved"))
                continue
            landed = batch_auth.action_landed("close", repo, close[1])
        except Exception as exc:  # noqa: BLE001 - reported, never silent
            found.append(("?", number, f"the close could not be checked ({type(exc).__name__})"))
            continue
        if landed is True:
            found.append((repo, close[1], None))
        elif landed is None:
            found.append((repo, close[1], "whether the close landed could not be read"))
    return found


def _open_dependents(repo: str, number: str, runner=None) -> list[str] | None:
    """Each open dependent as `owner/repo#n`, or None when the read failed."""
    runner = runner or subprocess.run
    try:
        done = runner(["gh", "api", "--paginate",
                       f"repos/{repo}/issues/{number}/dependencies/blocking",
                       "--jq", '.[] | select(.state == "open") | '
                               '"\\(.repository_url | split("/") | .[-2:] | join("/"))#\\(.number)"'],
                      capture_output=True, text=True, timeout=READ_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    return [line.strip() for line in done.stdout.splitlines() if line.strip()]


def lines(payload: dict, runner=None) -> list[str]:
    out: list[str] = []
    for repo, number, unknown in _closed(payload):
        if unknown:
            out.append(f"{repo}#{number} close; dependents UNKNOWN ({unknown})")
            continue
        dependents = _open_dependents(repo, number, runner)
        if dependents is None:
            out.append(f"{repo}#{number} closed; dependents UNKNOWN "
                       f"(dependencies/blocking could not be read)")
            continue
        out.extend(f"handoff now owed: {dependent} (its blocker {repo}#{number} closed)"
                   for dependent in dependents)
    return out


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(payload, dict) or payload.get("tool_name") not in (None, "Bash"):
        return 0
    try:
        found = lines(payload)
    except Exception:
        return 0
    if found:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "\n".join(found)}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
