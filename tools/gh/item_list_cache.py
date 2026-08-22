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

# harmonic-forge#329: both `gh_issue.py` and `model_tier_gate.py` independently
# hardcoded `--limit 1000` for a full board scan. hrse's board measured 721
# items live 2026-08-22 and is growing (docs/PRIORITIES.md); 1000 silently
# truncates the moment it's crossed, with no error -- a missed item just
# never resolves. One shared, generous constant beats two copies drifting
# out of sync the way `--limit 1000` itself already had. Still a fixed cap,
# not real pagination -- raise it again well before the board approaches it.
BOARD_ITEM_SCAN_LIMIT = 5000


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


def _issue_cache_file(
    repo: str, issue_number: int, project_number: str, cache_dir: Path
) -> Path:
    """Cache path for one issue's tier on one board (harmonic-forge#250).

    Keyed per (repo, issue, board) rather than per board, because that is the
    granularity the targeted read fetches. A board-wide key would invalidate
    every issue's entry whenever any one of them was re-read.

    The `tier__` prefix keeps these out of `_owner_number_glob`, so
    `invalidate()` -- which exists for board-wide writers -- does not silently
    match them with a different key shape.
    """
    safe_repo = _SAFE.sub("_", repo)
    safe_project = _SAFE.sub("_", str(project_number))
    return cache_dir / f"tier__{safe_repo}_{int(issue_number)}_{safe_project}.json"


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


# hrse#802: GitHub bills GraphQL on query *complexity* -- the number of nodes a
# query could return -- not on HTTP call count. `fetch_item_list` over a 542-item
# board costs hundreds of points; this targeted read costs roughly one, because
# it can only ever return a handful of nodes. Use it whenever the question is
# "what is field X on issue N", and reserve `fetch_item_list` for questions that
# genuinely need the whole board (drift checks, delta syncs).
#
# Live evidence for why this matters (2026-08-12): GraphQL went 444 -> 4,952 used
# in about an hour, dominated by ~6 full-board fetches in ~10 minutes, and the
# final `board_sync --apply` died mid-run on quota exhaustion. Note the compound:
# hrse#800 correctly raised the fetch from 500 to 542 items, which *raised*
# per-fetch cost -- making fetch frequency, not fetch correctness, the thing that
# now needs managing.
# harmonic-forge#257: reads one issue's Tier without fetching the board.
# This asked for Estimate alongside Tier while the rename was in flight, so the
# migration could be order-independent across two repos and a PreToolUse hook.
# Both boards are migrated and the Estimate field was deleted in hrse#966, so
# the second half is gone.
_TIER_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    issue(number: $number) {
      projectItems(first: 10) {
        nodes {
          project { number }
          tier: fieldValueByName(name: "Tier") {
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
        }
      }
    }
  }
}
"""

# `deep` is the escalating tier. It started at 8 points rather than 13 when it
# replaced the numeric threshold, so 8-point work kept requiring the high-tier
# model -- the regression harmonic-forge#257 existed to avoid. The points
# mapping itself is retired with the Estimate field (hrse#966).
TIER_FAST = "fast"
TIER_STANDARD = "standard"
TIER_DEEP = "deep"
ESCALATING_TIERS = frozenset({TIER_DEEP})


def fetch_issue_tier(
    repo: str,
    issue_number: int,
    project_number: str,
    run=None,
    ttl: float = 0,
    cache_dir: Path = None,
) -> str | None:
    """Return one issue's Tier (harmonic-forge#257).

    Returns None when the issue carries no Tier, or is not on this board --
    both are the same "nothing to gate on" verdict, which every caller already
    handles. The legacy numeric-Estimate fallback is retired: the field was
    deleted from both boards in hrse#966, so it could only ever have read a
    field that no longer exists.

    `ttl` > 0 caches the result on disk (harmonic-forge#250), mirroring
    `fetch_item_list`. This matters because the caller that needed the
    targeted read is a `PreToolUse` hook running on *every* tool call: the
    query is ~1 complexity point instead of a whole board, but uncached it
    would trade one cheap cached board fetch per TTL window for a network
    round-trip per keystroke-level operation -- fewer points, far more
    requests, and a latency cost on every edit. ttl<=0 keeps the previous
    always-live behaviour for callers that verify after a write.

    A cached `None` is a real answer ("no tier to gate on") and is cached as
    such. Only a *failure* is uncached -- GhItemListError propagates and
    nothing is written, so a quota blip can never be frozen into a 120s
    window of false "unset", which is the hrse#802 lesson.
    """
    resolved_cache_dir = cache_dir if cache_dir is not None else _CACHE_DIR
    cache_file = (
        _issue_cache_file(repo, issue_number, project_number, resolved_cache_dir)
        if ttl > 0 else None
    )
    if cache_file is not None:
        try:
            if cache_file.exists() and (time.time() - cache_file.stat().st_mtime) < ttl:
                with open(cache_file) as f:
                    return json.load(f).get("tier")
        except (OSError, json.JSONDecodeError, AttributeError):
            pass  # unreadable cache is a miss, never an error

    tier = _fetch_issue_tier_live(repo, issue_number, project_number, run)

    # Written only on a successful read. A GhItemListError propagates out of
    # the call above without touching the cache, so a transient failure is
    # never frozen into a TTL window of false "unset".
    if cache_file is not None:
        try:
            resolved_cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = cache_file.with_suffix(".json.tmp")
            with open(tmp, "w") as f:
                json.dump({"tier": tier}, f)
            tmp.replace(cache_file)  # atomic: a concurrent hook never reads a half-written file
        except OSError:
            pass  # an unwritable cache degrades to "uncached", never to an error
    return tier


def _fetch_issue_tier_live(
    repo: str,
    issue_number: int,
    project_number: str,
    run=None,
) -> str | None:
    """The uncached read. Split out so `fetch_issue_tier` has exactly one
    place to write the cache, rather than one per return path."""
    if run is None:
        import subprocess

        def run(args: list[str]):
            return subprocess.run(args, capture_output=True, text=True)

    try:
        owner, name = repo.split("/", 1)
    except ValueError:
        raise GhItemListError(f'repo must be "owner/name", got {repo!r}') from None

    result = run([
        "gh", "api", "graphql",
        "-f", f"query={_TIER_QUERY}",
        "-F", f"owner={owner}",
        "-F", f"repo={name}",
        "-F", f"number={int(issue_number)}",
    ])
    if result.returncode != 0:
        stderr = getattr(result, "stderr", None)
        raise GhItemListError(stderr.strip() if stderr else "gh api graphql failed")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise GhItemListError(f"unexpected response shape: {exc}") from exc
    if payload.get("errors"):
        messages = "; ".join(
            e.get("message", "?") for e in payload["errors"] if isinstance(e, dict)
        )
        raise GhItemListError(messages or "graphql returned errors")

    issue = (payload.get("data") or {}).get("repository", {}).get("issue")
    if not issue:
        return None

    nodes = ((issue.get("projectItems") or {}).get("nodes")) or []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if str((node.get("project") or {}).get("number")) != str(project_number):
            continue
        tier = (node.get("tier") or {}).get("name")
        if isinstance(tier, str) and tier:
            return tier.strip().lower()
        return None  # on this board, Tier unset
    return None  # not on this board


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
