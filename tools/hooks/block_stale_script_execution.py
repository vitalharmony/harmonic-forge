#!/usr/bin/env python3
"""PreToolUse hook: refuse to run a script from a checkout that lacks its fix.

hrse#861. Tooling validates the commit being *attested* -- `l1_post.py`
resolves the branch, checks it descends from `origin/main`, and builds a
detached worktree to run Tier 1 against it. Nothing validates the
checkout that will actually *run* anything.

Real near-miss, 2026-08-13. While preparing the Lane 3 execution handoff
for hrse#849, the shared main checkout was two commits behind at
`b3ccaa6` and its copy of `scripts/1-backfill_activity_direction.py` was
still the pre-REFORGE `5b9ab7f` version -- the thread-level unanimity
classifier hrse#849 had explicitly replaced, because 88% of the 219
candidate rows sit in multi-row threads that rule leaves indeterminate.

Running the migration from that checkout would have completed, reported
a plausible result, and left most rows unresolved. **No error, no
warning.** It was caught only because the handoff prep happened to
compare `git log` against the attested SHA by hand.

The check: for each script an invocation names, find the last commit on
`origin/main` that touched that path, and confirm the running checkout's
HEAD contains it. If not, the checkout is behind on that specific file.

Per-path rather than whole-worktree, deliberately: `HEAD != origin/main`
is normal and constant on any feature branch, so a whole-worktree check
would fire on nearly every invocation and be disabled within a day. A
path-scoped check only fires when the file about to run is genuinely
stale.

Severity is split by what the invocation would do. A write/apply mode
denies -- that is the case that silently corrupts a result. A read-only
or scan mode is allowed with a warning, because blocking exploratory
reads is how a hook earns its way out of the settings file.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from shell_parse import command_segments  # noqa: E402

# Interpreters whose next non-flag argument is the script being run.
INTERPRETERS = ("python", "python3", "bash", "sh")
INTERPRETER_PREFIXES = re.compile(r"^python3?(?:\.\d+)?$")

# Runners whose *second* token is the script: `uv run x.py`.
RUNNERS = {"uv": "run", "poetry": "run", "pipenv": "run", "hatch": "run"}

# Wrappers that precede the real program.
WRAPPERS = ("env", "sudo", "nohup", "time", "command", "exec", "stdbuf")
TIMEOUT_LIKE = ("timeout", "nice", "ionice")
ENV_ASSIGNMENT = re.compile(r"^\w+=")

# Short options that consume the following token.
VALUE_OPTS = ("-c", "-m", "-W", "-X", "-Q")

SCRIPT_SUFFIXES = (".py", ".sh", ".mjs", ".js", ".ts")

# Explicit write flags. Kept exact rather than substring-matched: matching
# the raw command denied `--report --thread-id 'skip --execute for now'`.
WRITE_FLAGS = ("--execute", "--apply", "--write", "--commit", "--no-dry-run",
               "--mutates-live")

# Read-only flags that exempt an otherwise write-by-default script.
READ_ONLY_FLAGS = ("--report", "--dry-run", "--check", "--verify", "--scan", "--list")

# CLAUDE.md: single-use migration scripts are prefixed `1-`. Thirteen of
# them write to the graph with no gating flag at all, so flag-only
# detection would give them a warning where a deny is wanted.
WRITE_BY_DEFAULT = re.compile(r"(?:^|/)[123]-[^/]+$")

# ...except the diagnostics. Four `1-*` scripts contain zero write Cypher
# and accept no flags at all, so they can never clear the exemption and
# would be denied rather than warned. A false deny on a read-only
# diagnostic is how a hook gets removed.
READ_ONLY_NAMES = re.compile(
    r"(?:^|/)[123]-(?:verify|diagnos|check|audit|report|retrigger|inspect)")

# Write-path scripts that carry no numeric prefix and no gating flag.
# `migrate_communication_channels.py` has 21 write statements and would
# otherwise be classified read-only.
WRITE_BY_NAME = re.compile(
    r"(?:^|/)(?:migrate|clean|seed|delete|patch|normalize|backfill)[_-]")

# Deliberate escape hatch. A worktree pinned to an older commit is a real
# workflow -- l1_post.py builds exactly one -- and a fail-closed check with
# no bypass gets deleted rather than fixed.
OVERRIDE_ENV = "HRSE_ALLOW_STALE_SCRIPT"

UPSTREAM = "origin/main"


def _allow() -> None:
    print(json.dumps({}))


def _warn(message: str) -> None:
    # systemMessage reaches the user's UI only. additionalContext is the
    # field injected into the model's context -- and the warning is
    # addressed to whoever interprets the run's output, which is the
    # model. Emitting only systemMessage made the read-only half of this
    # hook inert.
    print(json.dumps({
        "systemMessage": message,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": message,
        },
    }))


def _deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
        "systemMessage": reason,
    }))


def _git(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess[str] | None:
    if shutil.which("git") is None:
        return None
    try:
        return subprocess.run(
            ("git", *args), capture_output=True, text=True, timeout=7, cwd=cwd,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _strip_prefixes(tokens: list[str]) -> list[str]:
    """Drop env assignments and wrapper programs before the real command."""
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if ENV_ASSIGNMENT.match(token) or os.path.basename(token) in WRAPPERS:
            index += 1
            continue
        if os.path.basename(token) in TIMEOUT_LIKE:
            index += 1
            # `timeout 5 python3 ...` -- skip a bare numeric/duration arg.
            if index < len(tokens) and re.match(r"^[\d.]+[smhd]?$", tokens[index]):
                index += 1
            continue
        break
    return tokens[index:]


def _script_in_segment(tokens: list[str]) -> str | None:
    """The script this segment would execute, if any."""
    tokens = _strip_prefixes(tokens)
    if not tokens:
        return None

    def normalize(path: str) -> str:
        return path[2:] if path.startswith("./") else path

    program = os.path.basename(tokens[0])
    args = tokens[1:]

    if program in RUNNERS:
        if args and args[0] == RUNNERS[program]:
            args = args[1:]
        else:
            return None
    elif not (INTERPRETER_PREFIXES.match(program) or program in INTERPRETERS):
        # Direct execution: ./scripts/x.py
        return normalize(tokens[0]) if tokens[0].endswith(SCRIPT_SUFFIXES) else None

    skip_next = False
    for token in args:
        if skip_next:
            skip_next = False
            continue
        if token in VALUE_OPTS:
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        return normalize(token) if token.endswith(SCRIPT_SUFFIXES) else None
    return None


def segment_is_write(tokens: list[str], script: str) -> bool:
    """Whether THIS segment writes -- evaluated token-exact, not by substring.

    Substring-matching the whole command attributed one segment's
    `--execute` to another segment's read-only invocation, and matched a
    flag name appearing inside a quoted argument value.
    """
    flags = {t.split("=", 1)[0] for t in tokens if t.startswith("-")}
    if flags & set(WRITE_FLAGS):
        return True
    if flags & set(READ_ONLY_FLAGS):
        return False
    if READ_ONLY_NAMES.search(script):
        return False
    return bool(WRITE_BY_DEFAULT.search(script) or WRITE_BY_NAME.search(script))


def analyze(command: str, base_cwd: str | None) -> list[tuple[str, str | None, bool]] | None:
    """(script, effective_cwd, writes) per segment, or None if unparseable.

    Tracks `cd` between segments: `cd /tmp/impl && python3 scripts/x.py`
    runs the script in /tmp/impl, not in the payload's cwd. Ignoring that
    both denied correct commands and allowed genuinely stale ones.
    """
    try:
        segments = command_segments(command)
    except ValueError:
        return None

    cwd = base_cwd
    found: list[tuple[str, str | None, bool]] = []
    for tokens in segments:
        if not tokens:
            continue
        if tokens[0] == "cd" and len(tokens) > 1:
            target = os.path.expanduser(tokens[1])
            cwd = target if os.path.isabs(target) else (
                os.path.normpath(os.path.join(cwd, target)) if cwd else target)
            continue
        script = _script_in_segment(tokens)
        if script:
            found.append((script, cwd, segment_is_write(tokens, script)))
    return found


def upstream_commit_for(path: str, cwd: str | None) -> str | None:
    """Last commit on origin/main touching `path`.

    Assumes the local `origin/main` ref is reasonably fresh. This hook
    does not fetch -- a network call on every Bash invocation is not
    affordable -- so a checkout that has never fetched sees an
    `origin/main` at or behind its own HEAD and is silently allowed. The
    check makes staleness rare, not impossible.
    """
    result = _git("log", "-1", "--format=%H", UPSTREAM, "--", path, cwd=cwd)
    if result is None or result.returncode:
        return None
    return result.stdout.strip() or None


def head_contains(commit: str, cwd: str | None) -> bool:
    """True when HEAD already includes `commit`.

    Only exit status 1 means "not an ancestor". Any other nonzero status
    is an error -- a bad object in a shallow clone exits 128 -- and must
    fail open, or an unrelated git problem denies real work.
    """
    result = _git("merge-base", "--is-ancestor", commit, "HEAD", cwd=cwd)
    if result is None:
        return True
    return result.returncode != 1


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        _allow()
        return

    if data.get("tool_name") != "Bash":
        _allow()
        return

    if os.environ.get(OVERRIDE_ENV):
        _allow()
        return

    command = (data.get("tool_input") or {}).get("command", "")
    found = analyze(command, data.get("cwd") or None)
    if not found:
        _allow()
        return

    stale_write: list[tuple[str, str]] = []
    stale_read: list[tuple[str, str]] = []
    for script, cwd, writes in found:
        commit = upstream_commit_for(script, cwd)
        if not commit or head_contains(commit, cwd):
            continue
        (stale_write if writes else stale_read).append((script, commit))

    if not stale_write and not stale_read:
        _allow()
        return

    def describe(items: list[tuple[str, str]]) -> str:
        return "; ".join(
            f"{path} was last changed upstream in {commit[:7]}, which this "
            f"checkout's HEAD does not contain" for path, commit in items)

    if stale_write:
        _deny(
            f"Blocked: this checkout is behind on a script you are about to "
            f"run in write mode -- {describe(stale_write)} (hrse#861).\n\n"
            f"A stale script does not fail loudly. It runs to completion and "
            f"reports a plausible result computed by the old logic. On "
            f"2026-08-13 the shared checkout was two commits behind and held "
            f"the pre-REFORGE classifier hrse#849 had replaced; running it "
            f"would have left most of 219 rows unresolved and looked "
            f"successful.\n\n"
            f"Update the checkout, or run from a worktree at the attested "
            f"commit. If the older version is what you deliberately want -- a "
            f"bisect, or a worktree pinned to an attested SHA -- set "
            f"{OVERRIDE_ENV}=1 for that command."
        )
        return

    _warn(
        f"Warning: this checkout is behind -- {describe(stale_read)} "
        f"(hrse#861). Read-only invocation allowed, but results come from the "
        f"older logic -- do not treat this run as evidence."
    )


if __name__ == "__main__":
    main()
