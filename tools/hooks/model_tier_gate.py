#!/usr/bin/env python3
"""PreToolUse hook: require the high-tier model on estimate >= 8 issues.

harmonic-forge#202. Reads the PreToolUse JSON payload from stdin, resolves
the issue currently being worked on from the branch name in cwd, looks up
its board `Estimate` field, and denies a code-writing tool call if the
session's active model doesn't match the required tier.

Fail-open on every resolution failure (no branch match, no board access, no
gh, unexpected payload shape) -- this hook must never wedge a lane over its
own telemetry breaking. See 3-lane-protocol.md Tooling Exception.

Wired identically for Claude Code (Edit|Write matcher) and Codex
(apply_patch matcher) -- both feed the same PreToolUse JSON shape over
stdin, confirmed live 2026-08-09 (harmonic-forge#202 comments).
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

THRESHOLD = 8

# harmonic-forge#203: `gh project item-list --limit 1000` is the single most
# expensive call this hook makes (GitHub's GraphQL limiting is cost-based on
# query complexity/node count, confirmed live -- a handful of these calls
# fully drained a 5000-point quota). This hook runs as a fresh process on
# every single code-writing tool call, so an in-process cache is useless;
# a short-TTL on-disk cache collapses a whole editing burst into one real
# fetch. 120s is short enough that a mid-session re-estimation is picked up
# within about two minutes, long enough to absorb dozens of Edit/Write
# calls in a normal burst.
_CACHE_DIR = Path(tempfile.gettempdir()) / "harmonic-forge-gh-item-list-cache"
_CACHE_TTL = 120

# Claude Code model families are substring-matched against message.model
# (e.g. "claude-opus-5", "claude-sonnet-5"); Codex models are matched
# exactly against payload["model"] (e.g. "gpt-5.6-sol", "gpt-5.6-terra").
CLAUDE_HIGH = "opus"
CLAUDE_LOW = "sonnet"
CODEX_HIGH = "gpt-5.6-sol"
CODEX_LOW = "gpt-5.6-terra"

BRANCH_ISSUE_RE = re.compile(r"^[\w.-]+/[a-zA-Z]?(\d+)-")


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _allow() -> None:
    sys.exit(0)


def _deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def resolve_issue_number(cwd: str) -> int | None:
    result = _run(["git", "-C", cwd, "branch", "--show-current"])
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    m = BRANCH_ISSUE_RE.match(branch)
    return int(m.group(1)) if m else None


def _mise_env_value(mise_toml: str, key: str) -> str | None:
    m = re.search(rf'^{key}\s*=\s*"([^"]*)"', mise_toml, re.MULTILINE)
    return m.group(1) if m else None


def resolve_project_board(cwd: str) -> tuple[str, str] | None:
    """Read GH_PROJECT_OWNER/GH_PROJECT_NUMBER from the repo's own mise.toml.

    Deliberately does NOT read os.environ -- those vars are only correct if
    the calling shell already ran mise's cd-hook for this exact cwd, which
    is not guaranteed (confirmed live, harmonic-forge#202: a shell that had
    activated HRSE2's mise env leaked GH_PROJECT_NUMBER=1 into a hook
    invocation whose payload cwd was actually the harmonic-forge worktree).
    Reading the file directly removes that timing dependency.
    """
    root_result = _run(["git", "-C", cwd, "rev-parse", "--show-toplevel"])
    if root_result.returncode != 0:
        return None
    root = root_result.stdout.strip()
    try:
        with open(os.path.join(root, "mise.toml")) as f:
            toml_text = f.read()
    except OSError:
        return None
    owner = _mise_env_value(toml_text, "GH_PROJECT_OWNER")
    number = _mise_env_value(toml_text, "GH_PROJECT_NUMBER")
    return (owner, number) if owner and number else None


def _cached_item_list(owner: str, number: str, cache_dir: Path = _CACHE_DIR, ttl: float = _CACHE_TTL) -> list | None:
    """Fetch `gh project item-list`, cached on disk keyed by owner+number.

    Deliberately file-based, not in-process: this hook runs as a brand-new
    process on every single tool call, so nothing survives between
    invocations except the filesystem. Cache miss/expiry/failure all just
    fall through to a live fetch -- fail-open applies here too, a broken
    cache must never be worse than no cache.
    """
    key = hashlib.sha256(f"{owner}/{number}".encode()).hexdigest()[:16]
    cache_file = cache_dir / f"{key}.json"

    try:
        if cache_file.exists() and (time.time() - cache_file.stat().st_mtime) < ttl:
            with open(cache_file) as f:
                return json.load(f)
    except (OSError, json.JSONDecodeError):
        pass  # fall through to a live fetch

    result = _run([
        "gh", "project", "item-list", number, "--owner", owner,
        "--limit", "1000", "--format", "json",
    ])
    if result.returncode != 0:
        return None
    try:
        items = json.loads(result.stdout)["items"]
    except (json.JSONDecodeError, KeyError):
        return None

    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        with open(cache_file, "w") as f:
            json.dump(items, f)
    except OSError:
        pass  # caching is an optimization, not a requirement

    return items


def resolve_estimate(cwd: str, issue_number: int) -> int | None:
    board = resolve_project_board(cwd)
    if board is None:
        return None
    owner, number = board
    items = _cached_item_list(owner, number, cache_dir=_CACHE_DIR)
    if items is None:
        return None
    for item in items:
        content = item.get("content") or {}
        if content.get("number") == issue_number:
            est = item.get("estimate")
            return int(est) if isinstance(est, (int, float)) else None
    return None


def resolve_claude_model(transcript_path: str) -> str | None:
    try:
        with open(transcript_path) as f:
            lines = f.readlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        model = (entry.get("message") or {}).get("model")
        if model:
            return model
    return None


def required_tier_met(payload: dict, high_required: bool) -> bool:
    if "model" in payload:  # Codex: model is a direct field
        model = payload["model"]
        is_high = CODEX_HIGH in model
    else:  # Claude Code: model must be read from the transcript tail
        model = resolve_claude_model(payload.get("transcript_path", ""))
        if model is None:
            return True  # fail open -- can't resolve, don't block
        is_high = CLAUDE_HIGH in model
    return is_high if high_required else True


def _main() -> None:
    if os.environ.get("LANE_MODEL"):
        _allow()  # explicit operator override

    payload = json.loads(sys.stdin.read())
    if not isinstance(payload, dict):
        _allow()

    tool_name = payload.get("tool_name")
    if tool_name not in ("Edit", "Write", "MultiEdit", "apply_patch"):
        _allow()

    cwd = payload.get("cwd") or os.getcwd()
    issue_number = resolve_issue_number(cwd)
    if issue_number is None:
        _allow()

    estimate = resolve_estimate(cwd, issue_number)
    if estimate is None or estimate < THRESHOLD:
        _allow()

    if required_tier_met(payload, high_required=True):
        _allow()

    switch_cmd = "/model gpt-5.6-sol" if "model" in payload else "/model opus"
    _deny(
        f"Issue #{issue_number} is estimated at {estimate} points (>= {THRESHOLD}) "
        f"-- harmonic-forge#202 requires the high-tier model for this work. "
        f"Run `{switch_cmd}` and retry, or set LANE_MODEL to override."
    )


def main() -> None:
    # This must never wedge a lane over its own telemetry breaking -- any
    # unexpected exception (missing `git` on PATH, a malformed payload
    # shape, etc.) falls through to allow rather than crashing the hook
    # process, which would otherwise deny-by-side-effect (a nonzero exit
    # with no permissionDecision is not the same as an explicit allow, and
    # differs by host). Found live, harmonic-forge#202 verification pass.
    try:
        _main()
    except SystemExit:
        raise
    except Exception:
        _allow()


if __name__ == "__main__":
    main()
