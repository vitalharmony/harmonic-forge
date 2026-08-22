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
* **`gh issue close` and `gh pr merge` (any form) -- moved out entirely**
  (harmonic-forge#336's reforge, 2026-08-22). They used to live here as two
  of three rules; now `batch_gate.py`/`batch_auth.decide()` is the sole
  decision for both, on every invocation, because a static
  `permissions.ask` rule was found to always beat a hook's `allow`
  regardless of hook order or content -- so a `gh pr merge`/`gh issue
  close` command sitting under *both* this hook's unconditional `ask` and
  `batch_gate.py`'s conditional `allow` would always ask, permanently
  defeating any authorization mechanism. Two hooks independently deciding
  the same command class is undefined behavior under "strongest decision
  wins" composition; one hook now owns each class end to end. See
  `batch_auth.py`'s module docstring for the full rationale, including why
  that hook's fail-direction is the *opposite* of this one's (asks on
  anything unparseable, rather than allowing).

THE ASK SET -- now one thing
-----------------------------
`git clean -fd` / `-xfd` -- deletes UNTRACKED files. Never committed, so no
reflog, no object, nothing to recover from. The only truly destructive git
subcommand this hook still covers.

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


_RULES = (_check_git_clean,)


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
    _ask(" ".join(concerns))


if __name__ == "__main__":
    main()
