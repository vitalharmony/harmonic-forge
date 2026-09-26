#!/usr/bin/env python3
"""SessionStart hook: surface the lane launcher's checkout-refresh outcome
(harmonic-forge#761).

Claude-sessions-only, deliberately: Codex has no SessionStart event
(`.codex/hooks.json` carries no such hook), and Gemini has none either (its
context is carried by the `lane3-context` extension instead). For those two
CLIs, the launcher's own terminal line (`lane1`/`lane2`/`lane3`, printed
before the session starts) and the exported `LANE_REFRESH_*` environment are
the only carriers -- this hook is the Claude-specific third one, not a
replacement for either.

Reads `LANE` and `LANE_REFRESH_*` from the environment the launcher already
exported (a child process cannot see or alter values its parent didn't set,
so whatever this hook reads is exactly what the launcher recorded). Emits
nothing when `LANE` is unset -- a bare `claude` invocation outside any lane
launcher gets no lane-specific notice.
"""
import json
import os
import subprocess
import sys


def _changed_paths(from_sha: str, to_sha: str) -> list[str]:
    """Best-effort; never raises. Empty on any git failure (e.g. FROM/TO
    unresolvable in a fixture, or a shallow checkout missing one side)."""
    if not from_sha or not to_sha or from_sha == to_sha:
        return []
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", from_sha, to_sha],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


def build_notice(env: dict) -> str | None:
    lane = env.get("LANE", "")
    if not lane:
        return None

    status = env.get("LANE_REFRESH_STATUS", "")
    from_sha = env.get("LANE_REFRESH_FROM", "")
    to_sha = env.get("LANE_REFRESH_TO", "")

    lines = []
    if status:
        if status == "updated":
            lines.append(f"Lane launcher: checkout was behind and was updated at launch, {from_sha[:12]} -> {to_sha[:12]}.")
        elif status == "current":
            lines.append(f"Lane launcher: checkout is current at {to_sha[:12] or from_sha[:12]}.")
        elif status == "ack-stale":
            lines.append("Lane launcher: staleness acknowledged (--ack-stale); the checkout was not updated.")
        elif status == "skipped-dirty":
            lines.append("Lane launcher: tracked changes were present, so the checkout was NOT updated -- it may be stale.")
        elif status == "skipped-diverged":
            lines.append("Lane launcher: HEAD was not a clean refs/heads/main, so the checkout was NOT updated -- it may be stale.")
        elif status == "fetch-failed":
            lines.append("Lane launcher: could not reach or resolve origin/main, so the checkout was NOT updated -- it may be stale.")

    env_status = env.get("LANE_REFRESH_ENV", "")
    if env_status == "relinked":
        lines.append("backend/.env was relinked to the main checkout's file (it had drifted or was missing).")

    if lane == "1":
        lines.append("Open your first reply with: Run `mise run restart --no-bump --no-git`.")
        changed = _changed_paths(from_sha, to_sha)
        if any(p.startswith("backend/") or p.startswith("frontend/") for p in changed):
            lines.append("backend/frontend code changed in this update.")
    elif lane == "3":
        if status == "updated":
            lines.append(f"Gate report must state: worktree was behind and was updated at launch from {from_sha[:12]} to {to_sha[:12]}.")
        else:
            lines.append("Gate report must state: worktree was current at launch.")

    if not lines:
        return None
    return "\n".join(lines)


def main() -> int:
    notice = build_notice(dict(os.environ))
    if notice:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": notice,
            }
        }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
