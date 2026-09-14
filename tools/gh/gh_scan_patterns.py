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
counts as a "scan":

  - `tools/gh/gh_shim` -- a `~/.local/bin/gh` shim every lane's PATH resolves
    to instead of the real binary, so a scan run from *inside a script* (a
    subprocess call, no shell hook in the loop) is still caught. It is the
    single point that ever actually execs the real `gh`, so it is the single
    point that consumes the one-shot override (`consume_override()`).
  - `tools/hooks/guard_gh_rest_budget.py` -- a `PreToolUse` hook that catches
    the same scan shapes typed directly into a Bash tool call, before the
    shim even runs. It only *peeks* at the override (`override_present()`)
    to decide whether to let the command through to the shim -- it never
    consumes it. If it consumed too, a typed scan whose command never
    reaches `gh` (because the hook denies outright) would silently burn the
    grant without ever running anything, and a scan that *does* reach `gh`
    would then find the file already gone. One override, one consumption,
    at the layer that actually execs.

The override is a single-purpose bypass: when fresh, it unlocks one call to
the real `gh` unconditionally -- both the scan-shape check AND the budget
floor -- because the whole point is "the operator has decided this one call
is worth spending on, regardless of what it looks like or what's left in
the budget."

This module never denies or refuses anything itself -- like
`tools/hooks/shell_parse.py`, it only classifies. Denial policy belongs to
each caller.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import subprocess
import time
from pathlib import Path

_CACHE_DIR = Path(os.path.expanduser("~/.cache/harmonic-forge"))
_BUDGET_FILE = _CACHE_DIR / "gh_core_budget.json"
_BUDGET_LOCK_FILE = _CACHE_DIR / "gh_core_budget.json.lock"
_OVERRIDE_FILE = _CACHE_DIR / "gh_scan_override"
_OVERRIDE_LOCK_FILE = _CACHE_DIR / "gh_scan_override.lock"

_BUDGET_STALE_SECONDS = 60
_OVERRIDE_MAX_AGE_SECONDS = 10 * 60

_REAL_GH_PROBE_TARGET = "repos/vitalharmony/harmonic-forge"
_REAL_GH_TIMEOUT_SECONDS = 7

_ISSUES_SINGLE_ITEM = re.compile(r"^/?repos/[^/]+/[^/]+/(issues|pulls)/?(\?.*)?$")
_ISSUES_COMMENTS_LIST = re.compile(r"^/?repos/[^/]+/[^/]+/issues/comments/?(\?.*)?$")

_RATELIMIT_REMAINING = re.compile(r"(?im)^X-Ratelimit-Remaining:\s*(\d+)\s*$")
_RATELIMIT_RESET = re.compile(r"(?im)^X-Ratelimit-Reset:\s*(\d+)\s*$")

#: gh flags that consume the NEXT token as their value, when given as a
#: separate token (`-X GET`, not `-XGET`/`--method=GET`). Anything not in
#: this set is either a boolean flag (`--paginate`) or unrecognized, and is
#: dropped on its own -- unrecognized-but-value-taking is the failure mode
#: this table exists to close (harmonic-forge#650 preclose-check finding:
#: `-X GET repos/o/r/issues` was reading "GET" as the endpoint).
_FLAGS_WITH_SEPARATE_VALUE = {
    "-X", "--method",
    "-H", "--header",
    "-R", "--repo",
    "-f", "-F", "--field", "--raw-field",
    "-q", "--jq",
    "-t", "--template",
    "--hostname",
    "--input",
    "--cache",
}
#: Long-flag names that also accept the glued `--name=value` form. Short
#: flags' glued form (`-XGET`) is handled by prefix length, not by name.
_LONG_FLAGS_WITH_VALUE = tuple(f for f in _FLAGS_WITH_SEPARATE_VALUE if f.startswith("--"))
_SHORT_FLAGS_WITH_VALUE = tuple(f for f in _FLAGS_WITH_SEPARATE_VALUE if not f.startswith("--"))

#: Flags whose presence implies a non-GET request when no explicit method is
#: given -- matches `gh api`'s own real behavior (a body flag switches the
#: default method to POST).
_BODY_IMPLIES_POST = {"-f", "-F", "--field", "--raw-field", "--input"}


def _split_argv(args: list[str]) -> tuple[str, list[str]]:
    """Return `(method, positional_args)` for a `gh api ...`-shaped argv
    (or `("GET", args)` unchanged for a non-`api` subcommand, where method
    is irrelevant). Correctly skips a flag's value even when the flag takes
    one as a separate token, so a value never gets mistaken for the first
    positional (the endpoint, or a subcommand like `list`).
    """
    positional: list[str] = []
    method: str | None = None
    implies_post = False
    skip_next = False
    for token in args:
        if skip_next:
            skip_next = False
            continue
        if token in ("-X", "--method"):
            skip_next = True
            continue
        if token.startswith("--method="):
            method = token.split("=", 1)[1]
            continue
        if token.startswith("-X") and len(token) > 2:
            method = token[2:]
            continue
        if token in _FLAGS_WITH_SEPARATE_VALUE:
            if token in _BODY_IMPLIES_POST:
                implies_post = True
            skip_next = True
            continue
        if any(token.startswith(f + "=") for f in _LONG_FLAGS_WITH_VALUE):
            if any(token.startswith(f + "=") for f in _BODY_IMPLIES_POST if f.startswith("--")):
                implies_post = True
            continue
        if any(token.startswith(f) and len(token) > len(f) for f in _SHORT_FLAGS_WITH_VALUE):
            if any(token.startswith(f) and len(token) > len(f) for f in _BODY_IMPLIES_POST if not f.startswith("--")):
                implies_post = True
            continue
        if token.startswith("-"):
            # A boolean flag we don't specifically recognize (--paginate,
            # -i, --silent, etc.) -- drop it, take no value.
            continue
        positional.append(token)
    resolved_method = (method or ("POST" if implies_post else "GET")).upper()
    return resolved_method, positional


def scan_reason(argv: list[str]) -> str | None:
    """Classify a `gh` argument list (without the leading `gh` token).

    Returns a human-readable reason naming the matched pattern when `argv`
    is a full-board/full-thread-list-shaped scan; returns None for
    everything else, in particular single-item reads such as
    `api repos/o/r/issues/123` or `api repos/o/r/issues/123/comments`, and
    a write against a collection path such as `api repos/o/r/issues -X POST`
    (creating an issue is not a scan, regardless of the path it POSTs to).
    """
    if not argv:
        return None

    method, positional = _split_argv(argv)
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
                for flag in ("-f", "-F", "--field", "--raw-field"):
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
                if token in ("-f", "-F", "--field", "--raw-field") and i + 1 < len(argv):
                    if "search(" in argv[i + 1]:
                        return "`gh api graphql` with a `search(...)` query (search scan)"
            return None

        path = target.lstrip("/")
        if path.startswith("search/"):
            return "`gh api search/...` (REST search-endpoint scan)"

        # Everything below is a *read*-shaped classification: a write
        # against the same path (create/update via POST/PATCH/PUT/DELETE)
        # is not a scan, no matter which collection path it targets.
        if method != "GET":
            return None

        if _ISSUES_SINGLE_ITEM.match(target) or _ISSUES_SINGLE_ITEM.match("/" + path):
            return "`gh api repos/O/R/issues` or `/pulls` list (full-issue/PR-list scan)"
        if _ISSUES_COMMENTS_LIST.match(target) or _ISSUES_COMMENTS_LIST.match("/" + path):
            return "`gh api repos/O/R/issues/comments` (repo-wide comment-list scan)"
        return None

    if head == "project" and len(positional) >= 2 and positional[1] == "item-list":
        return "`gh project item-list` (full-board scan)"

    return None


def is_gh_invocation(token: str) -> bool:
    """True when `token` -- the program name of an invocation, however it
    was spelled on the command line -- resolves to `gh` by basename. Catches
    `/usr/bin/gh`, `./gh`, etc., not just the bare `gh` a literal string
    comparison would require (harmonic-forge#650 preclose-check finding).
    """
    try:
        return Path(token).name == "gh"
    except (OSError, ValueError):
        return token == "gh"


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


def _override_is_fresh_file() -> bool:
    """True iff the override path exists, is a REGULAR file (never a
    directory -- `mkdir -p` on the path must not count, harmonic-forge#650
    preclose-check finding), and is younger than the max age. Never mutates
    anything; callers decide whether to consume.
    """
    try:
        st = _OVERRIDE_FILE.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    return (time.time() - st.st_mtime) < _OVERRIDE_MAX_AGE_SECONDS


def override_present() -> bool:
    """Read-only peek: would `consume_override()` succeed right now? Never
    deletes the file. This is what `guard_gh_rest_budget.py` calls -- it
    only needs to decide whether to let a typed command through to the
    shim, which is the one place the grant is actually spent.
    """
    return _override_is_fresh_file()


def consume_override() -> bool:
    """One-shot bypass. True exactly once per human-created override file,
    within `_OVERRIDE_MAX_AGE_SECONDS` of its creation; deletes the file
    when (and only when) this call is the one that successfully claims it.

    Locked so two racing callers (e.g. two subagents both shelling out to
    `gh` in the same moment) can't both observe "fresh" before either
    deletes it (harmonic-forge#650 preclose-check finding) -- exactly one
    caller wins the lock first, sees the file, deletes it, and returns
    True; every other caller then finds it already gone and returns False.
    An unlink that fails for a reason other than "already gone" (e.g. a
    permissions error) is treated as NOT consumed, never as consumed
    on a technicality.
    """
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(_OVERRIDE_LOCK_FILE, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            st = _OVERRIDE_FILE.lstat()
        except OSError:
            return False
        if not stat.S_ISREG(st.st_mode):
            # A directory (or other non-regular path) at the override
            # location is never a valid grant, and is left untouched --
            # this function only ever deletes a file it created the
            # authority to delete by virtue of being a regular file.
            return False
        fresh = (time.time() - st.st_mtime) < _OVERRIDE_MAX_AGE_SECONDS
        # Consume (delete) unconditionally once we've observed a regular
        # file under the lock -- fresh or stale. A stale grant left on disk
        # is exactly the silent-lingering-grant failure mode the original
        # docstring warned about; only the return value distinguishes
        # "you got the scan" from "there was nothing left to give you."
        try:
            _OVERRIDE_FILE.unlink()
        except OSError:
            return False
        return fresh
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
