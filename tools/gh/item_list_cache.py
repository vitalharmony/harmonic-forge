"""Shared, on-disk TTL cache for `gh project item-list`, used by every
caller that reads a Projects v2 board (harmonic-forge#219, consolidating
4 previously-independent duplicate fetches).

Deliberately file-based, not in-process: several callers (model_tier_gate.py
in particular) run as a brand-new process on every invocation, so nothing
survives between calls except the filesystem.

Cache policy is per-caller, not uniform -- confirmed live (pitch-inspection,
2026-08-11) that a single shared TTL is unsafe: board_sync.py reads inside
a read-modify-write loop (a stale read causes it to either re-fire
already-applied item-edit mutations or skip a real pending one), and
l1_post.py reads immediately after a board write it needs to see. Callers
choose their own ttl (0 disables caching entirely) and board_sync.py must
call invalidate() around its own writes -- this module does not do that
automatically, since it has no way to know when a caller is about to mutate.
"""

import json
import re
import tempfile
import time
from pathlib import Path

# Deliberately NOT inside this repo's tree (pitch-inspection, 2026-08-11:
# a default under __file__'s parent silently writes untracked, ungitignored
# JSON board snapshots into the harmonic-forge working tree on every read --
# same tmp dir model_tier_gate.py already used pre-#219, now the one shared
# location for every caller).
_CACHE_DIR = Path(tempfile.gettempdir()) / "harmonic-forge-gh-item-list-cache"
PROJECT_OWNER = "vitalharmony"
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


class GhItemListError(Exception):
    """Raised when `gh project item-list` fails. Never fail-open here --
    each caller decides its own failure UX (model_tier_gate.py catches
    this and returns None to preserve its documented fail-open contract;
    l1_post.py/board_sync.py/board_drift_check.py let it propagate to
    their existing fail-loud paths)."""


def _cache_file(owner: str, number: str, limit: int, cache_dir: Path) -> Path:
    safe_owner = _SAFE.sub("_", owner)
    safe_number = _SAFE.sub("_", str(number))
    return cache_dir / f"{safe_owner}_{safe_number}_{limit}.json"


def _owner_number_glob(owner: str, number: str) -> str:
    safe_owner = _SAFE.sub("_", owner)
    safe_number = _SAFE.sub("_", str(number))
    return f"{safe_owner}_{safe_number}_*.json"


# hrse#800: this default was 500 when introduced by harmonic-forge#219's
# call-site consolidation -- not a deliberate quota guard, just the helper's
# default. It became a silent truncation the moment a board crossed 500 items
# (hrse board hit 541), because `gh project item-list` returns exactly `limit`
# rows with no indication that more exist. Callers then read the missing rows
# as "not on the board" and skipped them, while `board_drift_check` reported
# "no drift" over a board it could only see 92% of. Raised well above any
# current board, AND truncation is now detected rather than trusted -- see the
# `len(items) >= limit` check below, which is the part that actually prevents
# a recurrence when some future board crosses this bound too.
DEFAULT_ITEM_LIMIT = 5000


def fetch_item_list(
    number: str,
    owner: str = PROJECT_OWNER,
    limit: int = DEFAULT_ITEM_LIMIT,
    ttl: float = 0,
    run=None,
    cache_dir: Path = None,
) -> list[dict]:
    """Return the `items` list from `gh project item-list`, cached on disk
    for `ttl` seconds keyed by (owner, number, limit). ttl<=0 always fetches
    live (still goes through this one code path, just never reads/writes
    the cache) -- use this for verify-after-write callers.

    `run` lets a caller inject its own subprocess wrapper (some callers
    already have one with their own error-message conventions); defaults
    to a plain `subprocess.run`. `cache_dir` overrides the module-level
    `_CACHE_DIR` (test isolation; also lets a caller keep its own
    historical cache location for backward compatibility).
    """
    if run is None:
        import subprocess

        def run(args: list[str]):
            return subprocess.run(args, capture_output=True, text=True)

    resolved_cache_dir = cache_dir if cache_dir is not None else _CACHE_DIR
    cache_file = _cache_file(owner, number, limit, resolved_cache_dir) if ttl > 0 else None
    if cache_file is not None:
        try:
            if cache_file.exists() and (time.time() - cache_file.stat().st_mtime) < ttl:
                with open(cache_file) as f:
                    return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass  # fall through to a live fetch

    result = run([
        "gh", "project", "item-list", str(number), "--owner", owner,
        "--limit", str(limit), "--format", "json",
    ])
    if result.returncode != 0:
        stderr = getattr(result, "stderr", None)
        raise GhItemListError(stderr.strip() if stderr else "gh project item-list failed")
    try:
        items = json.loads(result.stdout)["items"]
    except (json.JSONDecodeError, KeyError) as exc:
        raise GhItemListError(f"unexpected response shape: {exc}") from exc

    # hrse#800: `gh project item-list` gives no "there are more" signal -- a
    # full page and an exactly-`limit`-sized board are indistinguishable in the
    # response. Fail loudly on the ambiguous case rather than hand back a list
    # the caller will read as complete. A partial fetch reported as success is
    # what turned this from a bug into a misleading one: callers treated the
    # unseen tail as "not on the board" and drift checks passed over it.
    if len(items) >= limit:
        raise GhItemListError(
            f"gh project item-list returned {len(items)} items at --limit {limit} "
            f"for {owner}/{number} -- the result may be truncated and cannot be "
            f"trusted as the full board. Raise DEFAULT_ITEM_LIMIT (or pass a "
            f"larger limit) and re-run; do not treat the returned rows as complete."
        )

    if cache_file is not None:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(cache_file, "w") as f:
                json.dump(items, f)
        except OSError:
            pass  # caching is an optimization, not a requirement

    return items


def invalidate(owner: str, number: str, cache_dir: Path = None) -> None:
    """Delete every cached entry for (owner, number), any limit. Callers
    that write to the board (board_sync.py) must call this both before
    their first item-edit and after their last -- before, so a run that
    crashes mid-loop leaves no stale cache; after, so a following read
    (e.g. board_drift_check.py in the same sweep) sees fresh data."""
    resolved_cache_dir = cache_dir if cache_dir is not None else _CACHE_DIR
    if not resolved_cache_dir.exists():
        return
    for f in resolved_cache_dir.glob(_owner_number_glob(owner, number)):
        f.unlink(missing_ok=True)
