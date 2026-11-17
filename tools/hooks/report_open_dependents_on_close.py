#!/usr/bin/env python3
"""PostToolUse Bash: closing a blocker reports what it unblocks (harmonic-forge#1381 AC3).

A `blocked-on-prerequisite` deferral is written as a native `blocked_by` link
(`gh_issue.py --blocked-by`). Nothing watches for the blocker closing, and this
adds no watcher: the close command is the moment the edge changes, so this hook
reads `dependencies/blocking` once, for that issue, right after the close.

- Positives only (sticky-wicket REFORGE, harmonic-forge#1381): this hook reports
  what it can see; it does not claim to see every close. Shell text has no end
  of shapes, so the guarantee that nothing is owed belongs to `repo_hygiene`'s
  unblocked-dependents report, which reads issue state.
- A close the hook recognizes is checked with `batch_auth.action_landed`
  (PostToolUse fires on failure too). One the read says did not happen prints
  nothing; one it cannot decide prints "dependents UNKNOWN".
- Each OPEN dependent whose blockers have ALL closed
  (`issue_dependencies_summary.blocked_by == 0`) becomes one "handoff now owed"
  line. A dependent still blocked by another open issue owes nothing yet.
- An unreadable `dependencies/blocking`, or a spent time budget, prints
  "dependents UNKNOWN", never silence.
- A command that mentions a close the hook could not parse (a shell `if`, a
  loop, a subshell) gets one catch-all "dependents UNKNOWN; run repo_hygiene"
  line instead of more parsing.
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

import re  # noqa: E402
import time  # noqa: E402

#: One landing read plus one dependents read per close.
READ_TIMEOUT = 20
#: This hook's timeout in `.claude/settings.json`.
HOOK_TIMEOUT = 60
#: Work stops this long before the hook's timeout, so the final print always runs.
BUDGET_SECONDS = HOOK_TIMEOUT - 10
#: Any mention of a close in the command text, recognized or not.
#: Any text that may close an issue. Broad on purpose (harmonic-forge#1381
#: pass 2, sticky-wicket PATCH): flags between `issue` and `close`, a quoted
#: `state="closed"`, and the GraphQL `closeIssue` mutation all count.
#: The gap between `issue` and `close` is bounded: an unbounded lazy gap
#: rescans the rest of the segment after every unmatched `issue` word, which is
#: quadratic on a long command, and a hook timeout is silence
#: (harmonic-forge#1381 post-verdict check).
_CLOSE_MENTION = re.compile(r"issue\b[^;&|\n]{0,200}?\bclose\b|state\W{0,3}closed|closeIssue")
CATCH_ALL = ("a close may have run that this hook could not parse; dependents "
             "UNKNOWN; run repo_hygiene (its unblocked-dependents report)")


#: `batch_auth.action_landed`'s own fixed timeout; a landing read starts only
#: when this much budget is left.
LANDING_TIMEOUT = 30


def _closed(payload: dict, deadline: float | None = None,
            clock=time.monotonic) -> list[tuple[str, str, str | None]]:
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
        if deadline is not None and deadline - clock() <= LANDING_TIMEOUT:
            # checked before repo resolution too, which may itself read gh
            # (harmonic-forge#1381 pass 1, cross-family finding 6)
            found.append((close[0] or "?", number, "time budget spent"))
            continue
        try:
            repo = record_tasklist_post._advisory_repo(segment, close[0], payload)
            if not repo or not close[1]:
                found.append((repo or "?", number, "the closed issue could not be resolved"))
                continue
            if deadline is not None and deadline - clock() <= LANDING_TIMEOUT:
                found.append((repo, close[1], "time budget spent"))
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


def _open_dependents(repo: str, number: str, runner=None,
                     timeout: float = READ_TIMEOUT) -> list[str] | None:
    """Each open dependent with no open blocker left, as `owner/repo#n`, or None
    when the read failed."""
    runner = runner or subprocess.run
    try:
        done = runner(["gh", "api", "--paginate",
                       f"repos/{repo}/issues/{number}/dependencies/blocking",
                       "--jq", '.[] | select(.state == "open" and '
                               '(.issue_dependencies_summary.blocked_by // 1) == 0) | '
                               '"\\(.repository_url | split("/") | .[-2:] | join("/"))#\\(.number)"'],
                      capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    return [line.strip() for line in done.stdout.splitlines() if line.strip()]


def lines(payload: dict, runner=None, clock=time.monotonic) -> list[str]:
    out: list[str] = []
    command = (payload.get("tool_input") or {}).get("command") or ""
    deadline = clock() + BUDGET_SECONDS
    closes = _closed(payload, deadline, clock)
    for repo, number, unknown in closes:
        left = deadline - clock()
        if unknown:
            out.append(f"{repo}#{number} close; dependents UNKNOWN ({unknown})")
            continue
        if left <= 1:
            out.append(f"{repo}#{number} closed; dependents UNKNOWN (time budget spent)")
            continue
        dependents = _open_dependents(repo, number, runner, timeout=min(READ_TIMEOUT, left))
        if dependents is None:
            out.append(f"{repo}#{number} closed; dependents UNKNOWN "
                       f"(dependencies/blocking could not be read)")
            continue
        out.extend(f"handoff now owed: {dependent} (its blocker {repo}#{number} closed)"
                   for dependent in dependents)
    if _unparsed(command):
        out.append(CATCH_ALL)
    return out


def _unparsed(command: str) -> bool:
    """Whether the command mentions a close this hook did not fully parse
    (harmonic-forge#1381 pass 2, sticky-wicket PATCH). Judged per segment, so
    the classifier can only switch the catch-all off for a segment it parsed
    whole; it never offsets a count from another segment. Plus one guard that
    compares the regex with itself: mentions in the raw command beyond those
    in the segments mean the segmenter dropped text."""
    import batch_auth  # noqa: PLC0415
    seen = 0
    for segment in command_segments(command):
        mentions = len(_CLOSE_MENTION.findall(" ".join(segment)))
        seen += mentions
        if not mentions:
            continue
        if mentions > 1 or not batch_auth.classify_issue_close(segment):
            return True
    return len(_CLOSE_MENTION.findall(command)) > seen


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(payload, dict) or payload.get("tool_name") not in (None, "Bash"):
        return 0
    try:
        found = lines(payload)
    except Exception:  # noqa: BLE001 - never silent on a close it could not finish
        command = (payload.get("tool_input") or {}).get("command") or ""
        found = [CATCH_ALL] if _CLOSE_MENTION.search(command) else []
    if found:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "\n".join(found)}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
