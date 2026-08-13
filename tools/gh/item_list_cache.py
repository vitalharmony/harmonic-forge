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
# harmonic-forge#257: asks for BOTH Tier and Estimate in one round trip, and
# prefers Tier when present. The rename spans two repos plus a PreToolUse hook
# that runs on every tool call, so an atomic cutover would guarantee that some
# checkout somewhere reads a field that does not exist yet. Requesting both
# makes migration order-independent; the Estimate half comes out once both
# repos and both boards are through.
_ESTIMATE_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    issue(number: $number) {
      projectItems(first: 10) {
        nodes {
          project { number }
          tier: fieldValueByName(name: "Tier") {
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
          estimate: fieldValueByName(name: "Estimate") {
            ... on ProjectV2ItemFieldNumberValue { number }
          }
        }
      }
    }
  }
}
"""

# The one mapping, defined once. `deep` starts at 8 rather than 13 because
# model_tier_gate.py escalates on `estimate >= 8`; putting 8 in `standard` would
# silently stop 8-point work requiring the high-tier model, which is the exact
# regression harmonic-forge#257 exists to avoid.
TIER_FAST = "fast"
TIER_STANDARD = "standard"
TIER_DEEP = "deep"
ESCALATING_TIERS = frozenset({TIER_DEEP})


def tier_for_points(points: float | None) -> str | None:
    """Legacy numeric estimate -> tier. Used while both fields coexist."""
    if points is None:
        return None
    if points >= 8:
        return TIER_DEEP
    if points >= 5:
        return TIER_STANDARD
    return TIER_FAST


def fetch_issue_estimate(
    repo: str,
    issue_number: int,
    project_number: str,
    run=None,
) -> int | None:
    """Return the Estimate field for one issue on one board, without
    fetching the board (hrse#802).

    `repo` is "owner/name". `project_number` selects which board to read
    when an issue sits on several -- an issue can legitimately be on more
    than one project, and reading "the first one" would silently return
    another board's Estimate.

    Deliberately uncached. This replaces a `ttl=0` full-board read whose
    live-ness requirement is real (harmonic-forge#219: the caller must see
    a board write from moments earlier), and that requirement survives the
    change -- a targeted live query is still live, it is just not a scan.

    Returns None for all three "no usable estimate" cases -- issue not on
    this board, item present but Estimate unset, or the field absent from
    the board schema. Callers already treat these identically (an unset
    estimate and an off-board issue are both "no estimate to gate on"),
    and collapsing them here keeps that contract rather than inventing a
    distinction no caller acts on.

    Raises GhItemListError on transport/shape failure, matching
    `fetch_item_list` so callers keep one except clause for board reads.
    """
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
        "-f", f"query={_ESTIMATE_QUERY}",
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

    # A GraphQL 200 can still carry errors alongside a null field; treat that as
    # a failure rather than silently reading it as "no estimate", which would
    # turn an auth/quota error into a false "estimate is unset" verdict.
    if payload.get("errors"):
        messages = "; ".join(
            e.get("message", "?") for e in payload["errors"] if isinstance(e, dict)
        )
        raise GhItemListError(messages or "graphql returned errors")

    try:
        issue = (payload.get("data") or {}).get("repository", {}).get("issue")
    except AttributeError as exc:
        raise GhItemListError(f"unexpected response shape: {exc}") from exc
    if not issue:
        return None  # issue does not exist / not visible -- same as "no estimate"

    nodes = ((issue.get("projectItems") or {}).get("nodes")) or []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        project = node.get("project") or {}
        if str(project.get("number")) != str(project_number):
            continue
        est = (node.get("estimate") or {}).get("number")
        return int(est) if isinstance(est, (int, float)) else None
    return None  # not on this board


def fetch_issue_tier(
    repo: str,
    issue_number: int,
    project_number: str,
    run=None,
) -> str | None:
    """Return one issue's Tier (harmonic-forge#257).

    Prefers the Tier field; falls back to deriving a tier from the legacy
    numeric Estimate when Tier is unset, so this is correct both before and
    after the board migration and in either order across repos.

    Returns None only when the issue carries neither — the same "nothing to
    gate on" verdict `fetch_issue_estimate` returns, which every caller already
    handles.
    """
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
        "-f", f"query={_ESTIMATE_QUERY}",
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
        est = (node.get("estimate") or {}).get("number")
        return tier_for_points(est) if isinstance(est, (int, float)) else None
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
