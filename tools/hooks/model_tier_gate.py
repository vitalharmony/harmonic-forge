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
# `deep` covers what `THRESHOLD = 8` used to: the legacy fallback in
# resolve_tier() maps >= 8 to deep, so the escalation boundary is unchanged by
# the rename. That equivalence is the thing to preserve if this is ever retuned.
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

    harmonic-forge#219: delegates the actual fetch/cache mechanics to the
    shared tools/gh/item_list_cache.py module; this wrapper's only job is
    preserving the fail-open contract (None on any failure, never raise)
    and this function's existing call signature (cache_dir/ttl overrides
    used directly by test_model_tier_gate.py).
    """
    if _item_list_cache is None:
        return None  # shared module unavailable -- fail-open, same as any other resolution failure
    try:
        return _item_list_cache.fetch_item_list(
            number, owner=owner, limit=_item_list_cache.BOARD_ITEM_SCAN_LIMIT,
            ttl=ttl, run=_run, cache_dir=cache_dir,
        )
    except _item_list_cache.GhItemListError:
        return None


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


def resolve_tier(cwd: str, issue_number: int) -> str | None:
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
    issue_number = resolve_issue_number(cwd)
    if issue_number is None:
        _allow()

    tier = resolve_tier(cwd, issue_number)
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
