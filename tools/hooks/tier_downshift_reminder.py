#!/usr/bin/env python3
"""Stop hook: remind high-tier lanes to downshift after deep work (F769).

A deep issue is "in hand" -- and the reminder stays silent -- when any of:

1. the session cwd is on a deep issue's branch;
2. this turn posted to a deep issue: the backstop's post **receipts**, plus the
   command-text `posted_targets` as a suppression-only hint, since here a wrong
   guess costs silence, never a false accusation (harmonic-forge#843 reforge);
3. this turn's Bash changed into a deep issue's worktree (`cd`/`pushd`,
   `git -C`, `mise -C`; AC3, every lane);
4. at `LANE=2`, an implementation worktree exists for an OPEN deep issue (AC2),
   so an idle turn while a deep issue is still owed does not read as "nothing in
   hand". It holds until R-0094's cleanup removes the worktree.

Probes 3 and 4 resolve worktrees locally through `worktree_issue` (timed git,
raising inside a tree), read Tier from the 120 s cache, and settle open state
for every deep candidate in **one batched GraphQL read per account**: no read
cap, no ordering heuristic. Any failure, timeout included, suppresses (AC4).
"""

from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))
import model_tier_gate  # noqa: E402
import session_model  # noqa: E402
import tier_model_stop_backstop as backstop  # noqa: E402
import tier_model_trigger_check  # noqa: E402

# Per-receipt uncached Tier reads in `_posted_deep` stay capped at 5 (NC4); past
# it the reminder is suppressed, which is fail-quiet.
_MAX_POST_TIER_READS = 5
_PROBE_DEADLINE_SECONDS = 4.0


class _ProbeDeadline(Exception):
    """Raised by the whole-probe deadline; caught as fail-quiet silence."""


def _on_deadline(_signum, _frame):
    raise _ProbeDeadline()


def _is_deep_branch(cwd: str) -> bool:
    target = model_tier_gate.resolve_issue_target(cwd)
    if target is None:
        return False
    number, repo_hint = target
    tier = model_tier_gate.resolve_tier(cwd, number, repo_hint, ttl=0)
    if tier is model_tier_gate.LOOKUP_FAILED:
        raise RuntimeError("branch tier lookup failed")
    return tier in model_tier_gate.ESCALATING_TIERS


_TURN_SCANS: dict[tuple, tuple] = {}


def _turn(transcript_path: str):
    """`(calls, truncated, receipts)` from ONE bounded tail read per hook run
    (NC6), shared by `_posted_deep` and probe 3. Keyed by the file's identity,
    so a changed transcript is re-read."""
    try:
        stat = os.stat(transcript_path)
    except OSError:
        return backstop.scan_turn(transcript_path, with_receipts=True)
    key = (transcript_path, stat.st_mtime_ns, stat.st_size)
    if key not in _TURN_SCANS:
        _TURN_SCANS.clear()
        _TURN_SCANS[key] = backstop.scan_turn(transcript_path, with_receipts=True)
    return _TURN_SCANS[key]


def _posted_deep(transcript_path: str, cwd: str) -> bool:
    boards = tier_model_trigger_check._boards()
    posted: list[tuple[str, int]] = []
    calls, truncated, receipts = _turn(transcript_path)
    if truncated:
        raise RuntimeError("turn scan truncated")
    def cwd_repo():
        repo = model_tier_gate.resolve_repo(cwd)
        if repo is None:
            raise RuntimeError("cwd repository unresolved")
        return repo

    for repo, number, _model in receipts:
        if (repo, number) not in posted:
            posted.append((repo, number))
    for command, _model, _tool_id in calls:  # the suppression-only hint
        for target in backstop.posted_targets(command, cwd_repo):
            if target not in posted:
                posted.append(target)
    if len(posted) > _MAX_POST_TIER_READS:
        raise RuntimeError("too many posted targets")
    for repo, number in posted:
        tier, _error = tier_model_trigger_check.lookup_tier(repo, number, boards)
        if tier is model_tier_gate.LOOKUP_FAILED:
            raise RuntimeError("posted tier lookup failed")
        if tier in model_tier_gate.ESCALATING_TIERS:
            return True
    return False


def _changed_dirs(command: str, cwd: str) -> list[str]:
    """Directories one Bash command changes into: `cd`/`pushd` (the shared
    `directory_change`), `git -C <dir>`, `mise -C <dir>`. A chained relative
    `cd a && cd b` resolves `b` against `a`; relative paths start from the
    payload cwd. A wrong guess here names a path that is checked with
    `isdir` and a real `rev-parse`, so it can only miss, never misattribute."""
    from block_lane1_status_claims import directory_change  # noqa: PLC0415
    from shell_parse import command_segments, strip_invocation_prefix  # noqa: PLC0415

    try:
        segments = command_segments(command)
    except ValueError:
        return []
    dirs: list[str] = []
    base = cwd
    for raw in segments:
        tokens = strip_invocation_prefix(raw)
        if not tokens:
            continue
        change = directory_change(tokens)
        found: list[str] = []
        if change and change[1] and change[0]:
            found.append(change[0])
        if os.path.basename(tokens[0]) in ("git", "mise"):
            value = backstop._flag(tokens, "-C")
            if value:
                found.append(value)
        for index, target in enumerate(found):
            path = os.path.expanduser(target)
            path = path if os.path.isabs(path) else os.path.join(base, path)
            dirs.append(path)
            if index == 0 and change and change[1] and change[0]:
                base = path
    return dirs


def _turn_candidates(transcript_path: str, cwd: str) -> list[tuple[str, int]]:
    """Probe 3 (AC3): issues of worktrees this turn's Bash changed into."""
    import worktree_issue  # noqa: PLC0415 -- inside the caller's guard

    calls, _truncated, _receipts = _turn(transcript_path)
    found: list[tuple[str, int]] = []
    for command, _model, _tool_id in calls:
        for directory in _changed_dirs(command, cwd):
            if not os.path.isdir(directory):
                continue
            lines = worktree_issue.git_lines(directory, "rev-parse", "--show-toplevel")
            pair = worktree_issue.issue_for_worktree(lines[0].strip()) if lines else None
            if pair and pair not in found:
                found.append(pair)
    return found


def _worktree_candidates(cwd: str, env) -> list[tuple[str, int]]:
    """Probe 4 (AC2, `LANE=2`): issues of every implementation worktree."""
    import worktree_issue  # noqa: PLC0415 -- inside the caller's guard

    forge = env.get("HARMONIC_FORGE_ROOT") or str(Path.home() / "harmonic-forge")
    found: list[tuple[str, int]] = []
    seen_paths: set[str] = set()
    for base in (cwd, forge):
        lines = worktree_issue.git_lines(base, "worktree", "list", "--porcelain") or []
        for line in lines:
            path = line[len("worktree "):] if line.startswith("worktree ") else ""
            if not path or path in seen_paths or not worktree_issue.is_impl_worktree(path):
                continue
            seen_paths.add(path)
            pair = worktree_issue.issue_for_worktree(path)
            if pair and pair not in found:
                found.append(pair)
    return found


def _open_states(pairs: list[tuple[str, int]]) -> dict[tuple[str, int], str | None]:
    """One batched GraphQL read per account (NC3b). Aliases are generated, since
    repo names contain `-`. A per-alias null is "unknown", never a failure of
    the others; stdout is parsed even when one inaccessible repo makes `gh`
    exit nonzero."""
    import worktree_issue  # noqa: PLC0415

    accounts = worktree_issue.repo_accounts()
    groups: dict[str | None, list[tuple[str, int]]] = {}
    for pair in pairs:
        groups.setdefault(accounts.get(pair[0]), []).append(pair)
    states: dict[tuple[str, int], str | None] = {}
    for account, group in groups.items():
        fields = []
        for i, (repo, number) in enumerate(group):
            owner, name = repo.split("/", 1)
            fields.append(f'c{i}: repository(owner: "{owner}", name: "{name}") '
                          f'{{ issue(number: {number}) {{ state }} }}')
        env = None
        if account:
            try:
                import hook_identity  # noqa: PLC0415

                env = hook_identity.slot_env(group[0][0])
            except Exception:  # noqa: BLE001 -- inherit the caller's identity
                env = None
        result = model_tier_gate.timed_run(
            ["gh", "api", "graphql", "-f", "query={ " + " ".join(fields) + " }"], env=env)
        try:
            data = (json.loads(result.stdout or "{}") or {}).get("data") or {}
        except json.JSONDecodeError:
            data = {}
        for i, pair in enumerate(group):
            node = data.get(f"c{i}") or {}
            issue = node.get("issue") or {}
            states[pair] = issue.get("state")
    return states


def _deep_open_in_hand(pairs: list[tuple[str, int]], boards: dict) -> bool:
    deep = []
    for repo, number in pairs:
        tier, _error = tier_model_trigger_check.lookup_tier(
            repo, number, boards, ttl=model_tier_gate._CACHE_TTL)
        if tier is model_tier_gate.LOOKUP_FAILED:
            raise RuntimeError("probe tier lookup failed")
        if tier in model_tier_gate.ESCALATING_TIERS:
            deep.append((repo, number))
    if not deep:
        return False
    states = _open_states(deep)
    if all(state is None for state in states.values()):
        raise RuntimeError("no open-state could be read")  # undecidable: suppress
    return any(state == "OPEN" for state in states.values())


def _probe_in_hand(transcript_path: str, cwd: str, env) -> bool:
    """Probes 3 and 4 under one whole-probe deadline."""
    boards = tier_model_trigger_check._boards()
    previous = signal.signal(signal.SIGALRM, _on_deadline)
    signal.setitimer(signal.ITIMER_REAL, _PROBE_DEADLINE_SECONDS)
    try:
        pairs = _turn_candidates(transcript_path, cwd)
        if env.get("LANE") == "2":
            pairs += [p for p in _worktree_candidates(cwd, env) if p not in pairs]
        return _deep_open_in_hand(pairs, boards)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def run(payload: dict, env: dict | None = None, model=None) -> dict | None:
    """Return the quiet reminder only when no deep issue is in hand."""
    env = os.environ if env is None else env
    if env.get("LANE") not in {"1", "2", "3"}:
        return None
    transcript_path = payload.get("transcript_path") or ""
    cwd = payload.get("cwd") or os.getcwd()
    if not transcript_path or not os.path.isfile(transcript_path):
        return None
    current = model if model is not None else session_model.current_model(
        transcript_path, cwd, payload.get("session_id"))
    if not model_tier_gate.claude_model_is_high(current):
        return None
    try:
        if (_is_deep_branch(cwd) or _posted_deep(transcript_path, cwd)
                or _probe_in_hand(transcript_path, cwd, env)):
            return None
    except Exception:  # noqa: BLE001 -- Stop hooks fail quiet (AC4; _ProbeDeadline included)
        return None
    return {"systemMessage": (
        f"This lane is on {current} with no deep-tier issue in hand. "
        "Switch down: /model sonnet"
    )}


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        if isinstance(payload, dict):
            result = run(payload)
            if result:
                print(json.dumps(result))
    except Exception:  # noqa: BLE001 -- Stop hooks fail quiet
        return


if __name__ == "__main__":
    main()
