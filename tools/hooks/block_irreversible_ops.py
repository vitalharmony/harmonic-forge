#!/usr/bin/env python3
"""Ask before genuinely unrecoverable operations (harmonic-forge#245).

Rewritten from scratch after a `sticky-wicket` verdict. The previous version
matched regexes against the whole command string and was wrong in both
directions -- it denied `grep -rn 'rm -rf' tools/` and passed `cd ~ && rm -rf .`.
Its self-test ratified that: one case asserted that writing the *sentence*
"git push --force" into a notes file should be challenged. 40/40 passed and the
first real command failed.

WHAT THIS HOOK IS FOR
---------------------
Agent **accident**, not a determined bypass. `Bash(bash *)`, `Bash(python3 *)`
and `Bash(xargs *)` remain allowed, so
`python3 -c "shutil.rmtree('/home/mmangus')"` passes every layer here. Chasing
adversarial completeness is explicitly out of scope; near-zero false positives
is the goal, because a floor that blocks routine work gets switched off
permanently rather than fixed.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
* **No `rm` handling at all.** `permissions.deny` owns recursive force-delete.
  That tier is evaluated ahead of the permission mode, is hard-block by design
  (correct for catastrophic operations), is the only tier ever verified firing
  live, and produced zero false positives. A `shlex` prototype fixed all five
  known false positives here and still mishandled `cd ~ && rm -rf .` and
  `rm -rf $(echo ~)`. Static parsing cannot resolve globs, command
  substitution, or an earlier `cd` -- so it must not try.
* **Nothing reflog-recoverable.** `git reset --hard`, `git branch -D`,
  `git stash drop` and force-push all leave the objects reachable from the
  reflog. A five-entry ask list gets used; a twelve-entry one gets switched off.

THE ASK SET -- three things, each unrecoverable for a different reason
---------------------------------------------------------------------
1. `git clean -fd` / `-xfd`  -- deletes UNTRACKED files. Never committed, so no
   reflog, no object, nothing to recover from. The only truly destructive git
   subcommand.
2. PR merge **with branch deletion** -- `--delete-branch`, or REST
   `delete_branch=true`. Silently CLOSES a stacked child PR whose base is the
   deleted branch; the child's review history does not come back.
3. Issue close -- social rather than data. Reopening restores the issue but not
   the notification, and the protocol requires a human close.

REST FORMS ARE PRIMARY. `rules/universal-agent.md` mandates
`gh api repos/O/R/issues/N -X PATCH -f state=closed` and lists `gh issue close`
in the AVOID column. Every earlier tier -- `permissions.ask`,
`autoMode.soft_deny`, and the old hook -- matched only the deprecated CLI form,
so the path agents are actually instructed to use was entirely unguarded.

Parsing is per-invocation over `shell_parse.command_segments`, which masks
heredoc bodies. Eight sibling hooks already import it; this one was the only
holdout, and harmonic-forge#478/#481/#167 are three prior instances of
whole-string regex failing on shell commands in this same directory.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shell_parse import command_segments  # noqa: E402
import batch_auth  # noqa: E402  (harmonic-forge#336)

# `repos/OWNER/REPO/issues/123` inside any argument, including a full URL.
API_ISSUE_PATH = re.compile(r"repos/([^/\s]+/[^/\s]+)/issues/(\d+)")
API_MERGE_PATH = re.compile(r"repos/([^/\s]+/[^/\s]+)/pulls/(\d+)/merge")
ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _allow() -> None:
    sys.exit(0)


def _ask(reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "ask",
        "permissionDecisionReason": reason,
    }}))
    sys.exit(0)


def _strip_invocation_prefix(tokens: list[str]) -> list[str]:
    """Drop `env` and leading VAR=value assignments before the program."""
    index = 0
    while index < len(tokens) and (
        tokens[index] == "env" or ENV_ASSIGNMENT.match(tokens[index])
    ):
        index += 1
    return tokens[index:]


def _program(tokens: list[str]) -> str:
    return os.path.basename(tokens[0]) if tokens else ""


def _method_is(tokens: list[str], want: str) -> bool:
    """Does this `gh api` invocation carry -X/--method <want>?

    Handles `-XPATCH`, `-X PATCH`, `-X=PATCH`, `--method PATCH`,
    `--method=PATCH` and flag reordering -- the same surface
    `block_data_migration_close.py` already covers, reused rather than
    re-derived.
    """
    want = want.upper()
    for i, token in enumerate(tokens):
        upper = token.upper()
        if upper in (f"-X{want}", f"--METHOD={want}", f"-X={want}"):
            return True
        if token in ("-X", "--method") and i + 1 < len(tokens):
            if tokens[i + 1].upper() == want:
                return True
    return False


def _has_field(tokens: list[str], assignment: str) -> bool:
    """Is `key=value` present as a field argument?

    shlex has already stripped quotes, so `state='closed'` and `state=closed`
    arrive here identically.
    """
    return any(t.replace(" ", "") == assignment for t in tokens)


# ---------------------------------------------------------------------------
# The three rules. Each returns a reason string, or None.
# ---------------------------------------------------------------------------

def _check_git_clean(tokens: list[str]) -> str | None:
    """`git clean` with both -f and -d. Deletes untracked files: no reflog."""
    if _program(tokens) != "git" or "clean" not in tokens[1:2]:
        return None
    force = directory = False
    for token in tokens[2:]:
        if token.startswith("--"):
            if token == "--force":
                force = True
            elif token == "--directory":
                directory = True
        elif token.startswith("-") and len(token) > 1:
            # Bundled short flags: -fd, -xfd, -fdx ...
            letters = set(token[1:])
            force = force or "f" in letters
            directory = directory or "d" in letters
    if force and directory:
        return ("`git clean` with -f and -d deletes UNTRACKED files. They were "
                "never committed, so there is no reflog and no object to "
                "recover -- this is the one genuinely unrecoverable git "
                "operation.")
    return None


def _check_pr_merge_delete(tokens: list[str]) -> str | None:
    """PR merge that also deletes the branch. Silently closes stacked children."""
    if _program(tokens) != "gh":
        return None
    rest = tokens[1:]

    # REST form first -- the one the rules mandate.
    if rest[:1] == ["api"]:
        if not any(API_MERGE_PATH.search(t) for t in rest):
            return None
        if not _method_is(rest, "PUT"):
            return None
        if _has_field(rest, "delete_branch=true"):
            return ("Merging a PR and deleting its branch silently CLOSES any "
                    "stacked child PR whose base is that branch. The child's "
                    "review history does not come back on reopen.")
        return None

    # CLI form, kept as secondary.
    if rest[:2] == ["pr", "merge"]:
        if any(t in ("--delete-branch", "-d") for t in rest[2:]):
            return ("`gh pr merge --delete-branch` silently CLOSES any stacked "
                    "child PR whose base is the deleted branch. Retarget the "
                    "child first.")
    return None


def _check_issue_close(tokens: list[str]) -> str | None:
    """Closing an issue. Social rather than data, and a human's call."""
    if _program(tokens) != "gh":
        return None
    rest = tokens[1:]

    if rest[:1] == ["api"]:
        if not any(API_ISSUE_PATH.search(t) for t in rest):
            return None
        if _method_is(rest, "PATCH") and _has_field(rest, "state=closed"):
            return ("Closing an issue. The protocol requires an explicit human "
                    "close -- Lane 3's gate or the operator's instruction, "
                    "never an agent's own judgement.")
        return None

    if rest[:2] == ["issue", "close"]:
        return ("Closing an issue. The protocol requires an explicit human "
                "close -- Lane 3's gate or the operator's instruction, never "
                "an agent's own judgement.")
    return None


_RULES = (_check_git_clean, _check_pr_merge_delete, _check_issue_close)


def find_concerns(command: str) -> list[str] | None:
    """Reasons this command should be challenged.

    Returns None when the command cannot be tokenized -- the caller treats that
    as fail-open. A hook that blocks whatever it cannot parse is a hook that
    gets unregistered.
    """
    try:
        segments = command_segments(command)
    except ValueError:
        return None

    concerns: list[str] = []
    for raw_tokens in segments:
        tokens = _strip_invocation_prefix(raw_tokens)
        if not tokens:
            continue
        for rule in _RULES:
            reason = rule(tokens)
            if reason and reason not in concerns:
                concerns.append(reason)
    return concerns


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        _allow()  # unparseable input is not a reason to block work

    if payload.get("tool_name") != "Bash":
        _allow()
    command = (payload.get("tool_input") or {}).get("command", "")
    if not command:
        _allow()

    concerns = find_concerns(command)
    if not concerns:
        _allow()
    matched, _reason = batch_auth.check_and_consume(command)
    if matched:
        _allow()  # covered by a live BATCH authorization (harmonic-forge#336)
    _ask(" ".join(concerns))


if __name__ == "__main__":
    main()
