#!/usr/bin/env python3
"""Stop hook: remind high-tier lanes to downshift after deep work (F769).

A deep issue is "in hand" -- and the reminder stays silent -- when any of:

1. the session cwd is on a deep issue's branch;
2. this turn posted to a deep issue (`backstop.posted_targets`, which since
   harmonic-forge#843 AC1 also reads `mise run l1-post`/`l2-post`);
3. this turn's Bash changed into a deep issue's worktree (`cd`/`pushd`,
   `git -C`, `mise -C`; harmonic-forge#843 AC3, every lane);
4. at `LANE=2`, an implementation worktree exists for an OPEN deep issue
   (harmonic-forge#843 AC2), so an idle turn while a deep issue is still owed
   (waiting on "Implement H<N>", answering a question) does not read as
   "nothing in hand". It holds until R-0094's cleanup removes the worktree.

Probes 3 and 4 share one budget of `_MAX_PROBE_READS` board or issue reads,
3 first because the directory a turn actually entered is the stronger signal,
and truncate rather than disable. Both run under one `_PROBE_DEADLINE_SECONDS`
deadline (`signal.setitimer`), because the belt's `resolve_worktree` they reuse
carries no per-call git timeout; the git calls added here use `timed_run`.
Every failure, timeout included, suppresses the reminder (AC4).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))
import model_tier_gate  # noqa: E402
import session_model  # noqa: E402
import tier_model_stop_backstop as backstop  # noqa: E402
import tier_model_trigger_check  # noqa: E402

_MAX_POST_TIER_READS = 5
_MAX_PROBE_READS = 5
_PROBE_DEADLINE_SECONDS = 3.0
_GIT_TIMEOUT_SECONDS = 1.5


class _ProbeDeadline(Exception):
    """Raised by the probe's deadline timer; caught as fail-quiet silence."""


def _on_deadline(_signum, _frame):
    raise _ProbeDeadline()


class _Budget:
    """Board and issue reads left for probes 3 and 4 together."""

    def __init__(self, reads: int):
        self.reads = reads

    def take(self) -> bool:
        if self.reads <= 0:
            return False
        self.reads -= 1
        return True


def _toplevel(directory: str) -> str | None:
    result = model_tier_gate.timed_run(
        ["git", "-C", directory, "rev-parse", "--show-toplevel"], timeout=_GIT_TIMEOUT_SECONDS)
    top = result.stdout.strip() if result.returncode == 0 else ""
    return top or None


def _deep_and_open(repo: str, number: int, boards: dict, budget: _Budget) -> bool:
    """Cached Tier first; the open-state read only for a deep candidate."""
    if not budget.take():
        return False
    tier, _error = tier_model_trigger_check.lookup_tier(
        repo, number, boards, ttl=model_tier_gate._CACHE_TTL)
    if tier is model_tier_gate.LOOKUP_FAILED:
        raise RuntimeError("probe tier lookup failed")
    if tier not in model_tier_gate.ESCALATING_TIERS or not budget.take():
        return False
    state = model_tier_gate.timed_run(
        ["gh", "api", f"repos/{repo}/issues/{number}", "--jq", ".state"])
    if state.returncode != 0:
        raise RuntimeError("probe issue state read failed")
    return state.stdout.strip() == "open"


def _changed_dirs(command: str, cwd: str) -> list[str]:
    """Directories one Bash command changes into: `cd`/`pushd` (the shared
    `directory_change`), `git -C <dir>` and `mise -C <dir>`. Relative paths
    resolve against the payload cwd, never this process's own."""
    try:
        segments = backstop.command_segments(command)
    except ValueError:
        return []
    dirs: list[str] = []
    for raw in segments:
        tokens = backstop.strip_invocation_prefix(raw)
        if not tokens:
            continue
        change = backstop.directory_change(tokens)
        found = [change[0]] if change and change[1] and change[0] else []
        if os.path.basename(tokens[0]) in ("git", "mise"):
            value = backstop._flag(tokens, "-C")
            if value:
                found.append(value)
        for target in found:
            path = os.path.expanduser(target)
            dirs.append(path if os.path.isabs(path) else os.path.join(cwd, path))
    return dirs


def _turn_dirs_deep(transcript_path: str, cwd: str, boards: dict, budget: _Budget) -> bool:
    """Probe 3 (AC3): this turn's Bash changed into a deep issue's worktree."""
    import worktree_issue  # noqa: PLC0415 -- inside the caller's guard

    calls, _truncated = backstop.scan_turn(transcript_path)
    seen: set[tuple[str, int]] = set()
    for command, _model, _tool_id in calls:
        for directory in _changed_dirs(command, cwd):
            if not os.path.isdir(directory):
                continue
            root = _toplevel(directory)
            pair = worktree_issue.issue_for_worktree(root) if root else None
            if pair is None or pair in seen:
                continue
            seen.add(pair)
            if _deep_and_open(*pair, boards, budget):
                return True
    return False


def _worktree_paths(directory: str) -> list[str]:
    result = model_tier_gate.timed_run(
        ["git", "-C", directory, "worktree", "list", "--porcelain"], timeout=_GIT_TIMEOUT_SECONDS)
    if result.returncode != 0:
        return []
    return [line[len("worktree "):] for line in result.stdout.splitlines()
            if line.startswith("worktree ")]


def _impl_worktrees_deep(cwd: str, env, boards: dict, budget: _Budget) -> bool:
    """Probe 4 (AC2, `LANE=2` only): an impl worktree for an open deep issue."""
    import worktree_issue  # noqa: PLC0415 -- inside the caller's guard

    forge = env.get("HARMONIC_FORGE_ROOT") or str(Path.home() / "harmonic-forge")
    paths: list[str] = []
    for base in (cwd, forge):
        for path in _worktree_paths(base):
            if path not in paths and worktree_issue.is_impl_worktree(path):
                paths.append(path)
    seen: set[tuple[str, int]] = set()
    for path in paths:
        pair = worktree_issue.issue_for_worktree(path)
        if pair is None or pair in seen:
            continue
        seen.add(pair)
        if _deep_and_open(*pair, boards, budget):
            return True
    return False


def _probe_in_hand(transcript_path: str, cwd: str, env) -> bool:
    """Probes 3 then 4, sharing one read budget, under one deadline."""
    boards = tier_model_trigger_check._boards()
    budget = _Budget(_MAX_PROBE_READS)
    previous = signal.signal(signal.SIGALRM, _on_deadline)
    signal.setitimer(signal.ITIMER_REAL, _PROBE_DEADLINE_SECONDS)
    try:
        if _turn_dirs_deep(transcript_path, cwd, boards, budget):
            return True
        return env.get("LANE") == "2" and _impl_worktrees_deep(cwd, env, boards, budget)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("probe git call timed out") from exc
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _is_deep_branch(cwd: str) -> bool:
    target = model_tier_gate.resolve_issue_target(cwd)
    if target is None:
        return False
    number, repo_hint = target
    tier = model_tier_gate.resolve_tier(cwd, number, repo_hint, ttl=0)
    if tier is model_tier_gate.LOOKUP_FAILED:
        raise RuntimeError("branch tier lookup failed")
    return tier in model_tier_gate.ESCALATING_TIERS


def _posted_deep(transcript_path: str, cwd: str) -> bool:
    boards = tier_model_trigger_check._boards()
    posted: list[tuple[str, int]] = []
    calls, truncated = backstop.scan_turn(transcript_path)
    if truncated:
        raise RuntimeError("turn scan truncated")
    def cwd_repo():
        repo = model_tier_gate.resolve_repo(cwd)
        if repo is None:
            raise RuntimeError("cwd repository unresolved")
        return repo

    for command, _model, _tool_id in calls:
        for target in backstop.posted_targets(command, cwd_repo, cwd):
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
    except (Exception, _ProbeDeadline):  # noqa: BLE001 -- Stop hooks fail quiet (AC4)
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
