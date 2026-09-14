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

WHY THE HOOK ONLY *PEEKS* AT THE OVERRIDE
-------------------------------------------
The contract is "one `touch ~/.cache/harmonic-forge/gh_scan_override`
unlocks the next `gh` call, whatever it is." Only `tools/gh/gh_shim` ever
actually execs the real `gh`, so it is the only layer that *consumes* the
override (`consume_override()`). This hook only asks "is a grant sitting
there right now" (`override_present()`), to decide whether to let a
scan-shaped command through to the shim -- it never deletes the file
itself. Consuming here too would double-spend or silently waste one grant:
a command this hook denies outright never reaches `gh` at all, so consuming
on the hook's own decision would burn the grant on a call that never ran;
and if the hook let the command through *without* deleting the file, the
shim then sees it fresh and consumes it there -- exactly once, at the layer
that actually executes.

The hook does NOT re-implement the budget-floor check -- that fires inside
the shim at actual execution time, and re-checking it here would just be a
second network round-trip for the same answer on every Bash call. This
hook's job is narrower: catch scan-shaped argv, an inline script or heredoc
that would hit the API directly, an under-interval `watch_lane_posts.py`
belt-mode invocation typed directly into Bash, and any attempt to
self-grant the override file.

MONITOR-ARMED WATCHERS AREN'T COVERED HERE, BY DESIGN
---------------------------------------------------------
This hook only fires on the `Bash` tool matcher, so a belt armed via the
`Monitor` tool (not a typed Bash command) never reaches `_watch_lane_posts_violation`
below. That gap is closed structurally, not by this hook, in
harmonic-forge#651's `CANONICAL_BELTS` argv enforcement built directly into
`watch_lane_posts.py` itself -- "This lives in the script, so it holds
whether launched via Bash, Monitor, subprocess or cron" (that issue's own
AC1). The check below stays as defense-in-depth for a directly-typed
command; it is not this hook's job to re-solve what #651 already solves
at the source.
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
#: Binaries/verbs safe to run against the override path without ever
#: creating or modifying it -- a narrow allowlist, checked only against the
#: first token of the segment. Everything else touching the path is denied
#: by default (harmonic-forge#650 preclose-check: the old check was an
#: allowlist of known WRITE shapes, and a `mkdir -p`, `env touch`, or a
#: `pathlib.Path.home()/....touch()` call matched none of them).
_OVERRIDE_READ_ONLY_BINARIES = ("cat", "stat", "ls", "test", "file", "wc", "head", "tail", "[")

_API_GITHUB_PATTERN = re.compile(r"api\.github\.com")
_FETCH_ITEM_LIST_PATTERN = re.compile(r"fetch_item_list\s*\(")
_TTL_ZERO_PATTERN = re.compile(r"ttl\s*=\s*0\b")
#: Matches `gh issue list` / `gh pr list` / `gh search` as either a shell
#: word sequence OR a Python list-literal sequence (`"gh","issue","list"`).
#: `_LIST_LITERAL_JUNK` strips comma/quote punctuation first so both forms
#: collapse to the same whitespace-separated shape before matching
#: (harmonic-forge#650 preclose-check: the un-widened regex needed literal
#: whitespace between `issue` and `list`, so `["gh","issue","list"]` --
#: AC6's own example -- passed uncaught).
_LIST_LITERAL_JUNK = re.compile(r"""['",]+""")
_GH_LIST_SEARCH_LITERAL = re.compile(
    r"""(?:^|\s)(?:gh\s+)?(?:issue\s+list|pr\s+list|search\b)"""
)
#: Raw HTTP libraries hitting the GitHub API directly, bypassing both guard
#: layers entirely (neither ever sees a `gh` invocation). Checked against
#: the RAW, un-heredoc-masked command text in `main()` -- `command_segments`
#: replaces a heredoc body with a placeholder specifically so its prose
#: isn't parsed as shell tokens, which also means no segment-level check
#: can ever see inside one. This is deliberately a separate, cruder,
#: whole-string scan for exactly that reason.
_RAW_HTTP_TO_GITHUB = re.compile(
    r"(?:requests\.(?:get|post|put|patch|delete)|urllib\.request\.urlopen|"
    r"httpx\.(?:get|post|put|patch|delete))\s*\([^)]*api\.github\.com"
)

_WATCH_LANE_POSTS_PATTERN = re.compile(r"watch_lane_posts\.py")
_INTERVAL_VALUE_PATTERN = re.compile(r"--interval[= ](\d+)")
_BELT_MODE_FLAGS = ("--queue-for", "--watch", "--all-worktrees", "--sweep-for")

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


def _is_override_write(tokens: list[str]) -> bool:
    """Any command that so much as REFERENCES the override path is denied,
    unless it is unambiguously read-only. The operator, never an agent, may
    grant this file, and the previous version of this check tried to name
    every way a write could look -- `touch`, `cp`, `>`, `open(`, `Path(`,
    `write_text`, `os.replace` -- which is exactly the game an agent (or a
    prompt-injected instruction) always wins eventually. `mkdir -p` on the
    path, `env touch`, and `pathlib.Path.home() / '...' ).touch()` all
    created the file and matched none of those names
    (harmonic-forge#650 preclose-check findings). Denying by default and
    carving out a narrow read-only allowlist closes the whole class instead
    of the three shapes anyone happened to test.
    """
    joined = " ".join(tokens)
    if not _OVERRIDE_WRITE_PATTERN.search(joined):
        return False
    first = tokens[0] if tokens else ""
    # Strip a leading `env VAR=val ...` or `sudo` wrapper before judging the
    # "binary" -- `env touch ~/.../gh_scan_override` must not read as `env`
    # being on some allowlist.
    idx = 0
    while idx < len(tokens) and (
        tokens[idx] in ("env", "sudo") or _ASSIGNMENT.match(tokens[idx] or "")
    ):
        idx += 1
    effective = tokens[idx] if idx < len(tokens) else first
    if effective in _OVERRIDE_READ_ONLY_BINARIES:
        # Still deny if there's any redirection into the path alongside the
        # read -- `cat x > gh_scan_override` is not read-only.
        if ">" in tokens or any(t.startswith(">") for t in tokens):
            return True
        return False
    return True


_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


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
    normalized = _LIST_LITERAL_JUNK.sub(" ", joined)
    if _GH_LIST_SEARCH_LITERAL.search(normalized):
        return (
            "inline script contains a `gh issue list`/`gh pr list`/`gh search` argument "
            "sequence, as a string literal or a Python list literal."
        )
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
    if stripped and gh_scan_patterns is not None and gh_scan_patterns.is_gh_invocation(stripped[0]):
        try:
            reason = gh_scan_patterns.scan_reason(stripped[1:])
        except Exception:
            reason = None
        if reason is not None:
            try:
                # Peek only -- do not consume. The shim consumes this same
                # grant when it actually execs `gh` (see the module
                # docstring's "WHY THE HOOK ONLY PEEKS" section above).
                overridden = gh_scan_patterns.override_present()
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
        # Checked against the RAW command text, before heredoc-masking --
        # `command_segments()` replaces a heredoc body with a placeholder
        # specifically so its prose isn't parsed as shell tokens, which
        # means a raw HTTP call to the GitHub API sitting inside one
        # (`python3 - <<EOF ... requests.get("https://api.github.com/...")
        # ... EOF`) is invisible to every segment-level check above,
        # AC2's own "heredoc bodies" language names this exact shape
        # (harmonic-forge#650 preclose-check finding).
        if _RAW_HTTP_TO_GITHUB.search(command):
            _deny(
                "Command contains a raw HTTP call to the GitHub API "
                "(requests/urllib/httpx), which neither guard layer can see "
                "once it runs -- this includes heredoc bodies."
            )
            return
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
