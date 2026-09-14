"""Shared scan-detection + budget/override state for the REST budget guard
(harmonic-forge#650).

WHY THIS EXISTS
-------------------
On 2026-09-14 the shared GitHub token's REST core budget hit
`X-Ratelimit-Used: 5000` about an hour into an orchestration session, and
every lane lost the ability to read or post to GitHub. `universal-agent.md`
R-0019/R-0020 already covered GraphQL; nothing covered REST list/search
endpoints, ad-hoc `gh api` scans, or scripts calling `gh` via subprocess.

Two independent enforcement layers share this module so they agree on what
counts as a "scan" and on the one-shot override semantics:

  - `tools/gh/gh_shim` -- a `~/.local/bin/gh` shim every lane's PATH resolves
    to instead of the real binary, so a scan run from *inside a script* (a
    subprocess call, no shell hook in the loop) is still caught.
  - `tools/hooks/guard_gh_rest_budget.py` -- a `PreToolUse` hook that catches
    the same scan shapes typed directly into a Bash tool call, before the
    shim even runs.

Both call `scan_reason()` for classification and `consume_override()` for
the one-shot bypass, so a single `touch
~/.cache/harmonic-forge/gh_scan_override` unlocks exactly one scan through
whichever layer the command actually reaches -- not one grant per layer.

This module never denies or refuses anything itself -- like
`tools/hooks/shell_parse.py`, it only classifies. Denial policy belongs to
each caller.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import time
from pathlib import Path

_CACHE_DIR = Path(os.path.expanduser("~/.cache/harmonic-forge"))
_BUDGET_FILE = _CACHE_DIR / "gh_core_budget.json"
_BUDGET_LOCK_FILE = _CACHE_DIR / "gh_core_budget.json.lock"
_OVERRIDE_FILE = _CACHE_DIR / "gh_scan_override"

_BUDGET_STALE_SECONDS = 60
_OVERRIDE_MAX_AGE_SECONDS = 10 * 60

_REAL_GH_PROBE_TARGET = "repos/vitalharmony/harmonic-forge"
_REAL_GH_TIMEOUT_SECONDS = 7

_ISSUES_SINGLE_ITEM = re.compile(r"^/?repos/[^/]+/[^/]+/(issues|pulls)/?(\?.*)?$")
_ISSUES_COMMENTS_LIST = re.compile(r"^/?repos/[^/]+/[^/]+/issues/comments/?(\?.*)?$")

_RATELIMIT_REMAINING = re.compile(r"(?im)^X-Ratelimit-Remaining:\s*(\d+)\s*$")
_RATELIMIT_RESET = re.compile(r"(?im)^X-Ratelimit-Reset:\s*(\d+)\s*$")

_FIELD_FLAGS = ("-f", "-F", "--field", "--raw-field")


def _strip_leading_flags(args: list[str]) -> list[str]:
    """Drop leading `-x`/`--xyz` tokens (and any value they take) so the
    first positional argument can be inspected. Deliberately shallow -- this
    is a classifier, not a full `gh` arg parser; it only needs to find the
    subcommand words and the first path-shaped positional.
    """
    out = []
    for token in args:
        if token.startswith("-"):
            continue
        out.append(token)
    return out


def scan_reason(argv: list[str]) -> str | None:
    """Classify a `gh` argument list (without the leading `gh` token).

    Returns a human-readable reason naming the matched pattern when `argv`
    is a full-board/full-thread-list-shaped scan; returns None for
    everything else, in particular single-item reads such as
    `api repos/o/r/issues/123` or `api repos/o/r/issues/123/comments`.
    """
    if not argv:
        return None

    positional = _strip_leading_flags(argv)
    if not positional:
        return None

    head = positional[0]

    if head == "issue" and len(positional) >= 2 and positional[1] == "list":
        return "`gh issue list` (full-issue-list scan)"
    if head == "pr" and len(positional) >= 2 and positional[1] == "list":
        return "`gh pr list` (full-PR-list scan)"
    if head == "search":
        return "`gh search ...` (search-endpoint scan)"

    if head == "api":
        rest = positional[1:]
        if not rest:
            return None
        target = rest[0]

        if target == "graphql":
            for token in argv:
                for flag in _FIELD_FLAGS:
                    value = None
                    if token == flag:
                        continue  # value is the *next* token; handled below
                    if token.startswith(flag) and len(token) > len(flag):
                        # glued form: -fquery=... / --field=...
                        value = token[len(flag):]
                        if value.startswith("="):
                            value = value[1:]
                    if value and "search(" in value:
                        return "`gh api graphql` with a `search(...)` query (search scan)"
            # separate-token form: -f query=...search(...)
            for i, token in enumerate(argv):
                if token in _FIELD_FLAGS and i + 1 < len(argv):
                    if "search(" in argv[i + 1]:
                        return "`gh api graphql` with a `search(...)` query (search scan)"
            return None

        path = target.lstrip("/")
        if path.startswith("search/"):
            return "`gh api search/...` (REST search-endpoint scan)"
        if _ISSUES_SINGLE_ITEM.match(target) or _ISSUES_SINGLE_ITEM.match("/" + path):
            return "`gh api repos/O/R/issues` or `/pulls` list (full-issue/PR-list scan)"
        if _ISSUES_COMMENTS_LIST.match(target) or _ISSUES_COMMENTS_LIST.match("/" + path):
            return "`gh api repos/O/R/issues/comments` (repo-wide comment-list scan)"
        return None

    if head == "project" and len(positional) >= 2 and positional[1] == "item-list":
        return "`gh project item-list` (full-board scan)"

    return None


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _read_budget_cache() -> dict | None:
    try:
        with open(_BUDGET_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _real_gh_path() -> str:
    """Resolve the real `gh` binary, never this repo's own shim."""
    shim_real = None
    try:
        shim_real = os.path.realpath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "gh_shim")
        )
    except OSError:
        shim_real = None
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(directory, "gh")
        if not os.path.isfile(candidate):
            continue
        try:
            real = os.path.realpath(candidate)
        except OSError:
            continue
        if shim_real is not None and real == shim_real:
            continue
        return candidate
    return "/usr/bin/gh"


def _fetch_budget_live() -> tuple[int, float] | None:
    real_gh = _real_gh_path()
    try:
        result = subprocess.run(
            [real_gh, "api", "-i", _REAL_GH_PROBE_TARGET],
            capture_output=True,
            text=True,
            timeout=_REAL_GH_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None

    # A 403 rate-limit response still carries these headers in its `-i`
    # output, regardless of exit code -- parse stdout unconditionally.
    output = result.stdout or ""
    remaining_match = _RATELIMIT_REMAINING.search(output)
    reset_match = _RATELIMIT_RESET.search(output)
    if not remaining_match or not reset_match:
        return None
    return int(remaining_match.group(1)), float(reset_match.group(1))


def budget() -> tuple[int, float] | None:
    """`(remaining, reset_epoch)` for the REST core budget, cached for
    `_BUDGET_STALE_SECONDS`. Returns None only when the headers genuinely
    can't be parsed (gh binary missing, no network, etc.) -- callers MUST
    treat None as fail-open, never as fail-closed.
    """
    cached = _read_budget_cache()
    if cached is not None:
        try:
            fetched_at = float(cached["fetched_at"])
            if time.time() - fetched_at < _BUDGET_STALE_SECONDS:
                return int(cached["remaining"]), float(cached["reset_epoch"])
        except (KeyError, TypeError, ValueError):
            pass

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(_BUDGET_LOCK_FILE, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        # Re-check under the lock: another process may have refreshed while
        # we waited for it.
        cached = _read_budget_cache()
        if cached is not None:
            try:
                fetched_at = float(cached["fetched_at"])
                if time.time() - fetched_at < _BUDGET_STALE_SECONDS:
                    return int(cached["remaining"]), float(cached["reset_epoch"])
            except (KeyError, TypeError, ValueError):
                pass

        fetched = _fetch_budget_live()
        if fetched is None:
            return None
        remaining, reset_epoch = fetched
        try:
            _atomic_write_json(
                _BUDGET_FILE,
                {"remaining": remaining, "reset_epoch": reset_epoch, "fetched_at": time.time()},
            )
        except OSError:
            pass  # an unwritable cache degrades to "uncached", never to an error
        return remaining, reset_epoch
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def consume_override() -> bool:
    """One-shot bypass. True exactly once per human-created override file,
    within 10 minutes of its creation; consumes (deletes) the file either
    way so a stale grant can't linger silently.
    """
    try:
        mtime = _OVERRIDE_FILE.stat().st_mtime
    except OSError:
        return False

    fresh = (time.time() - mtime) < _OVERRIDE_MAX_AGE_SECONDS
    try:
        _OVERRIDE_FILE.unlink()
    except OSError:
        pass
    return fresh
