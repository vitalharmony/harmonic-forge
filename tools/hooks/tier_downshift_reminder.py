#!/usr/bin/env python3
"""Stop hook: remind high-tier lanes to downshift after deep work (F769)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOKS_DIR))
import model_tier_gate  # noqa: E402
import session_model  # noqa: E402
import tier_model_stop_backstop as backstop  # noqa: E402
import tier_model_trigger_check  # noqa: E402


def _is_deep_branch(cwd: str) -> bool:
    target = model_tier_gate.resolve_issue_target(cwd)
    if target is None:
        return False
    number, repo_hint = target
    tier = model_tier_gate.resolve_tier(cwd, number, repo_hint)
    if tier is model_tier_gate.LOOKUP_FAILED:
        raise RuntimeError("branch tier lookup failed")
    return tier in model_tier_gate.ESCALATING_TIERS


def _posted_deep(transcript_path: str) -> bool:
    boards = tier_model_trigger_check._boards()
    posted: list[tuple[str, int]] = []
    for command, _model, _tool_id in backstop.turn_posts(transcript_path):
        for target in backstop.posted_targets(command, lambda: None):
            if target not in posted:
                posted.append(target)
    for repo, number in posted[:backstop._MAX_TIER_READS]:
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
    current = model if model is not None else session_model.current_model(
        transcript_path, cwd, payload.get("session_id"))
    if not model_tier_gate.claude_model_is_high(current):
        return None
    try:
        if _is_deep_branch(cwd) or _posted_deep(transcript_path):
            return None
    except Exception:  # noqa: BLE001 -- Stop hooks fail quiet
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
