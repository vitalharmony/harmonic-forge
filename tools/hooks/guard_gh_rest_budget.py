#!/usr/bin/env python3
"""`PreToolUse` guard: REST core-budget scans and overrides (harmonic-forge#650).

WHY A HOOK IN ADDITION TO THE SHIM
--------------------------------------
`tools/gh/gh_shim` catches every `gh` invocation at execution time,
including ones launched from inside a running script. This hook catches
the same scan *shapes* one step earlier -- typed directly into a Bash tool
call -- and denies rather than asking, because a scan-shaped `gh` command
here has already burned the budget by the time a human could review an
`ask` prompt under `--permission-mode auto` (every lane's default; `ask`
auto-approves under it, which is exactly the gap `block_raw_board_scan.py`
left open for this class of command).

Modeled on `block_raw_board_scan.py`: same `shell_parse.command_segments`/
`strip_invocation_prefix` usage, same fail-open convention (a malformed
payload or an internal exception must never wedge the session -- print a
visible systemMessage and let the tool call through, never crash).

WHY THE HOOK ALSO CONSUMES THE OVERRIDE
-------------------------------------------
The contract is "one `touch ~/.cache/harmonic-forge/gh_scan_override`
unlocks exactly one scan, through whichever layer the command actually
reaches." A typed `gh issue list` reaches this hook and never touches the
shim (this hook denies it outright, so `gh` never runs); a script that
calls `gh` via `subprocess` reaches only the shim. If this hook checked but
never consumed the override, a typed scan would deny with the override
present and unconsumed, matching the operator's intent -- but if it merely
observed without consuming, a second typed scan in the same session would
also pass, silently doubling the grant. So this hook calls
`consume_override()` itself, exactly like the shim, and both share the
same on-disk file: whichever layer's `scan_reason()` match fires first
consumes the one grant.

The hook does NOT re-implement the budget-floor check -- that fires inside
the shim at actual execution time, and re-checking it here would just be a
second network round-trip for the same answer on every Bash call. This
hook's job is narrower: catch scan-shaped argv, an inline script that would
hit the API directly, an under-interval `watch_lane_posts.py` belt-mode
invocation, and any attempt to self-grant the override file.
"""

from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gh"))

try:
    from shell_parse import command_segments, strip_invocation_prefix
except ImportError:  # pragma: no cover - the hook must never wedge a session
    command_segments = None
    strip_invocation_prefix = None

try:
    import gh_scan_patterns
except ImportError:  # pragma: no cover - the hook must never wedge a session
    gh_scan_patterns = None

_OVERRIDE_HINT = (
    "the operator can run `touch ~/.cache/harmonic-forge/gh_scan_override` "
    "to unlock one scan."
)

_OVERRIDE_WRITE_PATTERN = re.compile(
    r"gh_scan_override\b"
)

_API_GITHUB_PATTERN = re.compile(r"api\.github\.com")
_FETCH_ITEM_LIST_PATTERN = re.compile(r"fetch_item_list\s*\(")
_TTL_ZERO_PATTERN = re.compile(r"ttl\s*=\s*0\b")
_GH_LIST_SEARCH_LITERAL = re.compile(
    r"""['"]\s*(?:gh\s+)?(?:issue\s+list|pr\s+list|search\b)"""
)

_WATCH_LANE_POSTS_PATTERN = re.compile(r"watch_lane_posts\.py")
_INTERVAL_VALUE_PATTERN = re.compile(r"--interval[= ](\d+)")
_BELT_MODE_FLAGS = ("--queue-for", "--watch", "--all-worktrees")

_MIN_INTERVAL_SECONDS = 300


def _deny(reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": f"{reason}\n\n{_OVERRIDE_HINT}",
    }}))


def _allow() -> None:
    print(json.dumps({}))


def _fail_open(message: str) -> None:
    print(json.dumps({"systemMessage": f"guard_gh_rest_budget: {message}; guard did not run"}))


_WRITE_BINARIES = ("touch", "cp", "mv", "tee", "install", "ln")


def _is_override_write(tokens: list[str]) -> bool:
    """Any command that would create/touch/write the override path -- the
    operator, never an agent, may grant this file. Deliberately over-broad
    (a plain `cat`/read of the path is not itself a write, but ">"/write
    binaries/write-shaped Python calls are) rather than trying to be a
    precise filesystem-effect analyzer.
    """
    joined = " ".join(tokens)
    if not _OVERRIDE_WRITE_PATTERN.search(joined):
        return False
    if tokens and tokens[0] in _WRITE_BINARIES:
        return True
    if ">" in tokens or any(t.startswith(">") for t in tokens):
        return True
    if "os.replace" in joined or "open(" in joined or "Path(" in joined or "write_text" in joined:
        return True
    return False


def _watch_lane_posts_violation(tokens: list[str]) -> str | None:
    joined = " ".join(tokens)
    if not _WATCH_LANE_POSTS_PATTERN.search(joined):
        return None
    is_belt_mode = any(flag in joined for flag in _BELT_MODE_FLAGS)
    match = _INTERVAL_VALUE_PATTERN.search(joined)
    if match is not None:
        interval = int(match.group(1))
        if interval < _MIN_INTERVAL_SECONDS:
            return (
                f"`watch_lane_posts.py` invoked with `--interval {interval}`, below the "
                f"{_MIN_INTERVAL_SECONDS}s floor set after the 2026-09-14 REST budget "
                "exhaustion (harmonic-forge#650)."
            )
        return None
    if is_belt_mode:
        return (
            "`watch_lane_posts.py` invoked in belt-mode shape "
            f"({'/'.join(f for f in _BELT_MODE_FLAGS if f in joined)}) with no `--interval` "
            f"at all -- below the {_MIN_INTERVAL_SECONDS}s floor by default."
        )
    return None


def _inline_script_violation(tokens: list[str]) -> str | None:
    joined = " ".join(tokens)
    if "python3" not in joined and "python" not in joined:
        return None
    if "-c" not in tokens:
        return None
    if _API_GITHUB_PATTERN.search(joined):
        return "inline script targets `api.github.com` directly, bypassing both guard layers."
    if _FETCH_ITEM_LIST_PATTERN.search(joined) and _TTL_ZERO_PATTERN.search(joined):
        return "inline script calls `fetch_item_list(..., ttl=0)` -- an always-live full-board scan."
    if _GH_LIST_SEARCH_LITERAL.search(joined):
        return "inline script contains a `gh issue list`/`gh pr list`/`gh search` argument sequence as a string literal."
    return None


def _check_segment(tokens: list[str]) -> str | None:
    if not tokens:
        return None

    if _is_override_write(tokens):
        return (
            "This command would write/touch/create "
            "`~/.cache/harmonic-forge/gh_scan_override`. Agents may never grant "
            "themselves this override -- only the human operator may create that file."
        )

    stripped = strip_invocation_prefix(tokens)
    if stripped and stripped[0] == "gh" and gh_scan_patterns is not None:
        try:
            reason = gh_scan_patterns.scan_reason(stripped[1:])
        except Exception:
            reason = None
        if reason is not None:
            try:
                overridden = gh_scan_patterns.consume_override()
            except Exception:
                overridden = False
            if not overridden:
                return f"Scan-shaped `gh` command matched: {reason}."

    watch_violation = _watch_lane_posts_violation(tokens)
    if watch_violation is not None:
        return watch_violation

    inline_violation = _inline_script_violation(tokens)
    if inline_violation is not None:
        return inline_violation

    return None


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        _fail_open("malformed hook payload")
        return
    if not isinstance(payload, dict):
        _fail_open("hook payload was not an object")
        return

    command = ((payload.get("tool_input") or {}).get("command")) or ""
    if not command:
        _allow()
        return

    if command_segments is None or strip_invocation_prefix is None or gh_scan_patterns is None:
        _fail_open("shell_parse or gh_scan_patterns unavailable")
        return

    try:
        for segment in command_segments(command):
            reason = _check_segment(segment)
            if reason is not None:
                _deny(reason)
                return
    except Exception as exc:  # pragma: no cover - the hook must never wedge a session
        _fail_open(f"internal error ({exc!r})")
        return

    _allow()


if __name__ == "__main__":
    main()
