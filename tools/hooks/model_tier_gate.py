#!/usr/bin/env python3
"""PreToolUse hook: require the high-tier model on `deep`-tier issues.

harmonic-forge#202. Reads the PreToolUse JSON payload from stdin, resolves
the issue currently being worked on from the branch name in cwd, looks up
its board `Tier` field, and denies a code-writing tool call if the
session's active model doesn't match the required tier.

Fail-open on every resolution failure (no branch match, no board access, no
gh, unexpected payload shape) -- this hook must never wedge a lane over its
own telemetry breaking. See 3-lane-protocol.md Tooling Exception.

Wired identically for Claude Code (Edit|Write matcher) and Codex
(apply_patch matcher) -- both feed the same PreToolUse JSON shape over
stdin, confirmed live 2026-08-09 (harmonic-forge#202 comments).
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gh"))
try:
    import item_list_cache as _item_list_cache
except ImportError:
    _item_list_cache = None

# harmonic-forge#257: which Tier values require the high-tier model. Defined
# locally rather than imported from item_list_cache, because this hook must keep
# working when that module is not importable — the `except ImportError` above is
# the fail-open path, and a NameError here would deny-by-crash on every
# Edit/Write instead.
#
# `deep` preserves the former >= 8 escalation boundary. Tier is now the only
# routing input; do not add a second, numeric fallback here.
ESCALATING_TIERS = frozenset({"deep"})

# harmonic-forge#203: `gh project item-list --limit 1000` is the single most
# expensive call this hook makes (GitHub's GraphQL limiting is cost-based on
# query complexity/node count, confirmed live -- a handful of these calls
# fully drained a 5000-point quota). This hook runs as a fresh process on
# every single code-writing tool call, so an in-process cache is useless;
# a short-TTL on-disk cache collapses a whole editing burst into one real
# fetch. 120s is short enough that a mid-session re-estimation is picked up
# within about two minutes, long enough to absorb dozens of Edit/Write
# calls in a normal burst.
#
# Known tradeoff (harmonic-forge#202's own guarantee, narrowed slightly):
# a mid-lane re-estimation that crosses the >= 8 threshold can be silently
# missed by the gate for up to this TTL after the board write. Accepted as
# the cost of the burst-collapse -- fail-open already tolerates larger,
# unbounded gaps (missing gh entirely), so a bounded ~2min window is a
# strict improvement, not a new class of risk.
#
# harmonic-forge#219: the actual fetch/cache-file mechanics now live in
# tools/gh/item_list_cache.py, shared with board_sync.py/board_drift_check.py/
# l1_post.py (previously 4 independent duplicate implementations). This
# module keeps its own cache directory/TTL constants and fail-open wrapper
# unchanged -- only the fetch internals moved.
_CACHE_DIR = Path(tempfile.gettempdir()) / "harmonic-forge-gh-item-list-cache"
_CACHE_TTL = 120

# Claude Code model families are substring-matched against message.model
# (e.g. "claude-opus-5", "claude-sonnet-5"); Codex models are matched
# exactly against payload["model"] (e.g. "gpt-5.6-sol", "gpt-5.6-terra").
# Families that satisfy a `deep`-tier requirement -- Opus and above.
# harmonic-forge#314: this was a single `CLAUDE_HIGH = "opus"` substring,
# correct only while Opus was the family's ceiling. Fable 5 is above Opus,
# not a sibling, and `"opus" in "claude-fable-5"` is False -- so a
# deep-tier issue denied the most capable model available and told it to
# `/model opus`, i.e. to downgrade, on the work that most needs capability.
#
# Deliberately an explicit allowlist, not a wider pattern: model names are
# fluid as new models ship, and a new top tier should require a reviewed
# one-line addition here rather than being granted implicitly by a match
# that happens to be broad enough. Do not seed it with speculative names.
CLAUDE_HIGH_FAMILIES = frozenset({"opus", "fable"})
CODEX_HIGH = "gpt-5.6-sol"

# The former `CLAUDE_LOW`/`CODEX_LOW` constants are gone, not relocated.
# Both were defined and never read: the gate only ever asks "is this high
# enough", so "not high" is the complete answer, and a positive low-tier
# assertion would be a second independently-driftable list of names to
# maintain -- the same staleness this issue was filed for.

BRANCH_ISSUE_RE = re.compile(r"^[\w.-]+/[a-zA-Z]?(\d+)-")
HINTED_BRANCH_ISSUE_RE = re.compile(
    r"^[\w.-]+/(?P<hint>hrse|harmonic-forge|forge|h|f)-?(?P<number>\d+)-"
)
WORKTREE_ISSUE_RE = re.compile(r"^/tmp/hrse2-(?P<number>\d+)-impl$")
HINTED_TARGETS = {
    "hrse": ("vitalharmony/hrse", "1"), "h": ("vitalharmony/hrse", "1"),
    "harmonic-forge": ("vitalharmony/harmonic-forge", "3"),
    "forge": ("vitalharmony/harmonic-forge", "3"), "f": ("vitalharmony/harmonic-forge", "3"),
}


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


def resolve_issue_target(cwd: str) -> tuple[int, str | None] | None:
    result = _run(["git", "-C", cwd, "branch", "--show-current"])
    if result.returncode == 0:
        branch = result.stdout.strip()
        hinted = HINTED_BRANCH_ISSUE_RE.match(branch)
        if hinted:
            return int(hinted.group("number")), hinted.group("hint")
        m = BRANCH_ISSUE_RE.match(branch)
        if m:
            return int(m.group(1)), None
    worktree = WORKTREE_ISSUE_RE.match(os.path.realpath(cwd))
    return (int(worktree.group("number")), "hrse") if worktree else None


def resolve_issue_number(cwd: str) -> int | None:
    target = resolve_issue_target(cwd)
    return target[0] if target else None


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


def resolve_repo(cwd: str) -> str | None:
    """Read GH_REPO from the repo's own mise.toml (harmonic-forge#250).

    The targeted per-issue query needs "owner/name", which
    `resolve_project_board` does not supply -- it returns the *project* owner
    and number. Both hrse and harmonic-forge already declare GH_REPO in
    mise.toml, so this reuses the same file-reading path for the same reason
    `resolve_project_board` documents: os.environ is only correct if the
    calling shell ran mise's cd-hook for this exact cwd, which a hook
    invocation cannot assume (harmonic-forge#202, confirmed live).
    """
    root_result = _run(["git", "-C", cwd, "rev-parse", "--show-toplevel"])
    if root_result.returncode != 0:
        return None
    try:
        with open(os.path.join(root_result.stdout.strip(), "mise.toml")) as f:
            return _mise_env_value(f.read(), "GH_REPO")
    except OSError:
        return None


def resolve_tier(cwd: str, issue_number: int, repo_hint: str | None = None) -> str | None:
    """Return the issue's Tier (harmonic-forge#257).

    harmonic-forge#250: reads *one issue* rather than fetching the whole
    board. `gh project item-list --limit 1000` was the single most expensive
    call this hook made, and it made it from a `PreToolUse` hook -- a fresh
    process on every code-writing tool call. The targeted GraphQL read costs
    roughly one complexity point instead of hundreds.

    The disk cache is kept, and that is deliberate rather than incidental:
    a targeted-but-uncached read would swap one cheap cached board fetch per
    TTL window for a network round-trip on every single edit -- fewer quota
    points, far more requests, and latency on every operation. Cheap *and*
    cached is the point.

    Returns None when the issue carries no tier, when the board or repo
    cannot be resolved, or when the lookup fails -- all of which must keep
    meaning allow, not deny: this hook fires on every Edit/Write and must
    never wedge a lane over its own telemetry.
    """
    if _item_list_cache is None:
        return None  # shared module unavailable -- fail-open
    hinted_target = HINTED_TARGETS.get(repo_hint or "")
    if hinted_target:
        repo, number = hinted_target
    else:
        board = resolve_project_board(cwd)
        if board is None:
            return None
        repo = resolve_repo(cwd)
        if repo is None:
            return None
        _owner, number = board
    try:
        return _item_list_cache.fetch_issue_tier(
            repo, issue_number, number,
            run=_run, ttl=_CACHE_TTL, cache_dir=_CACHE_DIR,
        )
    except _item_list_cache.GhItemListError:
        return None


def _tail_lines(path: str, chunk_size: int = 65536, max_bytes: int = 4 << 20):
    """Yield lines from the end of a file backwards, in bounded chunks.

    harmonic-forge#314 (C4): this used `readlines()`, pulling the whole
    transcript into memory on every Edit/Write/MultiEdit call to use only
    the last model-bearing line. Local transcripts reach 107 MB, so that
    cost was paid on every code-writing tool call in a long session.

    Gives up after `max_bytes`. A transcript whose last 4 MB carries no
    `message.model` line then falls through to the caller's fail-open
    path -- the same outcome the old code gave for a file with no model
    line at all, at bounded cost instead of unbounded.
    """
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        remainder = b""
        scanned = 0
        while position > 0 and scanned < max_bytes:
            read_size = min(chunk_size, position)
            position -= read_size
            scanned += read_size
            handle.seek(position)
            block = handle.read(read_size) + remainder
            parts = block.split(b"\n")
            # parts[0] may be a partial line whose head is in a chunk we
            # have not read yet -- hold it back until we have that chunk.
            remainder = parts[0] if position > 0 else b""
            tail = parts[1:] if position > 0 else parts
            for line in reversed(tail):
                if line.strip():
                    yield line.decode("utf-8", "replace")


def resolve_claude_model(transcript_path: str) -> str | None:
    try:
        for line in _tail_lines(transcript_path):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            model = (entry.get("message") or {}).get("model")
            if model:
                return model
    except OSError:
        return None
    return None


def required_tier_met(payload: dict, high_required: bool) -> bool:
    if "model" in payload:  # Codex: model is a direct field
        model = payload["model"]
        is_high = CODEX_HIGH in model
    else:  # Claude Code: model must be read from the transcript tail
        model = resolve_claude_model(payload.get("transcript_path", ""))
        if model is None:
            return True  # fail open -- can't resolve, don't block
        is_high = any(family in model for family in CLAUDE_HIGH_FAMILIES)
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
    target = resolve_issue_target(cwd)
    if target is None:
        _allow()
    issue_number, repo_hint = target

    tier = resolve_tier(cwd, issue_number, repo_hint)
    if tier not in ESCALATING_TIERS:
        _allow()

    if required_tier_met(payload, high_required=True):
        _allow()

    switch_cmd = "/model gpt-5.6-sol" if "model" in payload else "/model opus"
    _deny(
        f"Issue #{issue_number} is Tier '{tier}' "
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
