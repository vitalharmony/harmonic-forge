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
import sys
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
    repo: str, issue_number: int, project_number: str, cache_dir: Path,
    field: str = "Tier",
) -> Path:
    """Cache path for one issue's FIELD on one board (harmonic-forge#250/#468).

    Keyed per (repo, issue, board) rather than per board, because that is the
    granularity the targeted read fetches. A board-wide key would invalidate
    every issue's entry whenever any one of them was re-read.

    The `field__` prefix keeps these out of `_owner_number_glob`, so
    `invalidate()` -- which exists for board-wide writers -- does not silently
    match them with a different key shape.

    harmonic-forge#468: the field name is part of the key. Without it, reading
    `Sequence` would overwrite the cached `Tier` for the same issue and the gate
    would read a sequence number as a tier — the generalization's one real
    hazard, closed here rather than left to callers.
    """
    safe_repo = _SAFE.sub("_", repo)
    safe_project = _SAFE.sub("_", str(project_number))
    safe_field = _SAFE.sub("_", field).lower()
    return cache_dir / f"field__{safe_field}__{safe_repo}_{int(issue_number)}_{safe_project}.json"


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
#: harmonic-forge#468. Was 5000 with `ttl=0` — "pull every item, do not read
#: the cache" as the *default*, which burned the GraphQL quota to zero twice
#: (2026-08-12 and 2026-09-04). The defaults are now the cheap path and the
#: expensive one is opt-in, which is the inversion AC1 asks for.
DEFAULT_ITEM_LIMIT = 5000

#: Default cache window for the mandated accessor. Ten minutes: long enough that
#: a burst of reads in one task costs one fetch, short enough that a stale board
#: is a nuisance rather than a wrong answer — and the currency probe below
#: usually makes the window moot by detecting an actual change first.
DEFAULT_TTL_SECONDS = 600

#: Repeat full scans inside this window are refused (AC6). The 2026-09-04
#: incident was two whole-board pulls **twelve minutes apart**; the module's own
#: comment names the compound — raising the item limit raised per-fetch cost, so
#: fetch *frequency* is the thing left to manage. Fifteen minutes would have
#: prevented that incident outright.
FULL_SCAN_COOLDOWN_SECONDS = 900


class FullScanTooSoon(GhItemListError):
    """A second full board scan inside `FULL_SCAN_COOLDOWN_SECONDS` (AC6).

    A subclass of `GhItemListError` so existing callers' error handling still
    catches it, but distinguishable for a caller that wants to fall back to the
    cached copy rather than fail.
    """


#: The currency probe (AC3). One GraphQL point for BOTH boards — measured
#: 2026-09-05, `rateLimit.cost: 1` — against hundreds for a single full scan.
#:
#: AC3 asked whether a cheap conditional check exists "through `gh project`".
#: It does not: `gh project item-list` has no conditional mode at all. It exists
#: through GraphQL, which this module already uses for the targeted field read
#: below, so this adds no new dependency.
#:
#: "Current" = `(updatedAt, totalCount)` unchanged since the cached copy was
#: written. `totalCount` catches adds and removes; `updatedAt` is what covers an
#: in-place field edit, which a count cannot see.
#:
#: **`updatedAt` does move on an item field write — measured, not assumed.**
#: The controlled test, 2026-09-05 on board 3: read `updatedAt`, set one issue's
#: `Tier` via `updateProjectV2ItemFieldValue`, read it again.
#:
#:     before  updatedAt 2026-09-05T01:05:45Z  totalCount 300
#:     after   updatedAt 2026-09-05T02:11:33Z  totalCount 300
#:
#: The timestamp moved and the count did not, which is exactly the case a count
#: alone cannot detect and the reason both halves are stored.
#:
#: **Do not check quota with `gh api rate_limit` — it does not report this
#: bucket correctly.** Measured back to back in the same second:
#:
#:     gh api rate_limit  -> graphql {used: 0,  remaining: 5000}
#:     graphql rateLimit  -> {used: 10, remaining: 4990}
#:     X-Ratelimit-Used on a real graphql call -> 11 (resource: graphql)
#:
#: The REST endpoint reported a full budget while GraphQL's own counter and the
#: response headers agreed it was spent. That is the answer to "why did a
#: mutation hit a wall while `rate_limit` showed headroom": same bucket, wrong
#: meter. Read `rateLimit` inside a GraphQL query, or the `X-Ratelimit-*`
#: response headers.
_BOARD_CURRENCY_QUERY = """
query($owner: String!, $number: Int!) {
  user(login: $owner) {
    projectV2(number: $number) {
      updatedAt
      items(first: 1) { totalCount }
    }
  }
}
"""


def fetch_full_board(
    number: str,
    owner: str = PROJECT_OWNER,
    limit: int = DEFAULT_ITEM_LIMIT,
    ttl: float = DEFAULT_TTL_SECONDS,
    run=None,
    cache_dir: Path = None,
    force: bool = False,
) -> list[dict]:
    """**The expensive one.** Pull every item on a board (AC2).

    Named for its cost. This is hundreds of GraphQL complexity points per call
    and is the operation that zeroed the quota twice; `get_board_items()` below
    is the mandated way in, and this should be reached only when the question
    genuinely needs the whole board — a drift check or a delta sync.

    Refuses a second full scan inside `FULL_SCAN_COOLDOWN_SECONDS` (AC6) unless
    `force=True`, raising `FullScanTooSoon`. The 2026-09-04 incident was two
    whole-board pulls twelve minutes apart; the cooldown is the mechanism that
    makes that impossible rather than merely discouraged.

    `ttl` now defaults to a real window (AC1) — the old default was `0`, meaning
    "never read the cache", so the obvious call was also the most expensive
    possible one. Verify-after-write callers pass `ttl=0` explicitly, which is
    the inverse of the old contract.

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
    # The path is computed unconditionally. `ttl <= 0` means "do not READ or
    # WRITE the cache" — it must not also mean "skip the cooldown". A `ttl=0`
    # caller is the MORE dangerous one for AC6's purpose, because it never
    # serves from cache and so can only ever scan.
    scan_marker = _cache_file(owner, number, limit, resolved_cache_dir)
    cache_file = scan_marker if ttl > 0 else None
    if cache_file is not None:
        try:
            if cache_file.exists() and (time.time() - cache_file.stat().st_mtime) < ttl:
                with open(cache_file) as f:
                    return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass  # fall through to a live fetch

    # AC6. Checked against the cache file's own mtime — the record of when this
    # board was last fully scanned — so it holds across processes and across
    # separate agent sessions, which is where the repeat scans came from.
    if not force:
        try:
            if scan_marker.exists():
                age = time.time() - scan_marker.stat().st_mtime
                if age < FULL_SCAN_COOLDOWN_SECONDS:
                    raise FullScanTooSoon(
                        f"{owner}/{number} was fully scanned {int(age)}s ago and the "
                        f"cooldown is {FULL_SCAN_COOLDOWN_SECONDS}s. A full board scan "
                        f"costs hundreds of GraphQL points and two in one window is what "
                        f"zeroed the quota on 2026-08-12 and 2026-09-04. Use "
                        f"get_board_items() for a cached read, fetch_issue_field() for "
                        f"one field on one issue, or pass force=True if this really "
                        f"needs a second full scan now."
                    )
        except OSError:
            pass  # an unstattable cache file is a miss, never a block

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


def _board_currency(owner: str, number: str, run=None) -> dict | None:
    """`{updatedAt, totalCount}` for one board, or None if the probe failed.

    One GraphQL point (AC3). Returns None rather than raising: a failed probe
    must degrade to the TTL behaviour, never block a read — the whole point is
    to spend less, and a probe that can wedge a caller is worse than no probe.
    """
    if run is None:
        import subprocess

        def run(args: list[str]):
            return subprocess.run(args, capture_output=True, text=True)

    result = run([
        "gh", "api", "graphql",
        "-f", f"query={_BOARD_CURRENCY_QUERY}",
        "-F", f"owner={owner}", "-F", f"number={int(number)}",
    ])
    if getattr(result, "returncode", 1) != 0:
        return None
    try:
        project = json.loads(result.stdout)["data"]["user"]["projectV2"]
        return {
            "updated_at": project["updatedAt"],
            "total_count": project["items"]["totalCount"],
        }
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def get_board_items(
    number: str,
    owner: str = PROJECT_OWNER,
    limit: int = DEFAULT_ITEM_LIMIT,
    ttl: float = DEFAULT_TTL_SECONDS,
    run=None,
    cache_dir: Path = None,
    check_currency: bool = True,
) -> list[dict]:
    """**The mandated way to read a board** (AC2).

    Serves the cached copy when it is current; refreshes only when it is not.
    "Current" is checked rather than assumed (AC3): a one-point GraphQL probe
    reads the board's `(updatedAt, totalCount)` and compares them to what was
    recorded when the cache was written. An unchanged pair means the cached rows
    are still right regardless of how old they are, so a long-lived board costs
    one point per read instead of a full scan per TTL window.

    The TTL is the fallback, not the mechanism: when the probe fails — offline,
    quota exhausted, an unexpected response shape — this degrades to plain
    TTL-expiry behaviour rather than failing or blind-refetching.

    Prefer `fetch_issue_field()` when the question is "what is field X on issue
    N". This still costs a full scan on a miss; that one costs about one point
    whatever the board's size.
    """
    resolved_cache_dir = cache_dir if cache_dir is not None else _CACHE_DIR
    cache_file = _cache_file(owner, number, limit, resolved_cache_dir)
    stamp_file = cache_file.with_suffix(".currency.json")

    cached: list[dict] | None = None
    try:
        if cache_file.exists():
            with open(cache_file) as f:
                cached = json.load(f)
    except (OSError, json.JSONDecodeError):
        cached = None

    if cached is not None and check_currency:
        live = _board_currency(owner, number, run)
        if live is not None:
            try:
                with open(stamp_file) as f:
                    recorded = json.load(f)
                if recorded == live:
                    return cached  # provably current — no scan at any age
            except (OSError, json.JSONDecodeError):
                pass  # no stamp recorded: fall through to the TTL check
        else:
            # Probe unavailable. Fall back to TTL, and say so — a currency check
            # that silently stopped checking is the failure mode this issue is
            # about (harmonic-forge#440's lesson).
            print(
                f"item_list_cache: currency probe unavailable for {owner}/{number}; "
                f"falling back to TTL",
                file=sys.stderr,
            )
        try:
            if (time.time() - cache_file.stat().st_mtime) < ttl:
                return cached
        except OSError:
            pass

    items = fetch_full_board(
        number, owner=owner, limit=limit, ttl=0, run=run,
        cache_dir=resolved_cache_dir, force=True,
    )

    # Written together with the rows they describe, so a later probe compares
    # against the state the cached copy actually reflects.
    try:
        resolved_cache_dir.mkdir(parents=True, exist_ok=True)
        with open(cache_file, "w") as f:
            json.dump(items, f)
        live = _board_currency(owner, number, run)
        if live is not None:
            with open(stamp_file, "w") as f:
                json.dump(live, f)
    except OSError:
        pass  # caching is an optimization, not a requirement

    return items


#: harmonic-forge#468: kept as a deprecated alias rather than deleted. It has no
#: production callers in either repo — every real consumer already uses the
#: targeted read — but ad-hoc scripts and agent muscle memory point at this
#: name, and an alias costs nothing. New code should call `get_board_items()`
#: or, better, `fetch_issue_field()`.
def fetch_item_list(*args, **kwargs) -> list[dict]:
    """Deprecated: use `get_board_items()` (cached) or `fetch_full_board()`.

    Preserves the OLD default of `ttl=0` so an existing ad-hoc caller's
    behaviour does not change silently underneath it — the defaults were
    inverted for new code, not retroactively for callers written against the
    old contract.
    """
    kwargs.setdefault("ttl", 0)
    kwargs.setdefault("force", True)
    return fetch_full_board(*args, **kwargs)


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
#: harmonic-forge#468 AC4: generalized from Tier-only to any single field.
#:
#: **All four value fragments, not just single-select.** The Tier-only version
#: read `ProjectV2ItemFieldSingleSelectValue` alone — and `Sequence`, the exact
#: field the 2026-09-04 quota incident was asking about ("what are the lowest
#: Sequence values in use?"), is a **number** field. A single-select-only read
#: would have answered `None` for the very question that caused the burn, and
#: sent the caller straight back to a full board scan.
_FIELD_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $field: String!) {
  repository(owner: $owner, name: $repo) {
    issue(number: $number) {
      projectItems(first: 10) {
        nodes {
          project { number }
          value: fieldValueByName(name: $field) {
            ... on ProjectV2ItemFieldSingleSelectValue { name }
            ... on ProjectV2ItemFieldTextValue { text }
            ... on ProjectV2ItemFieldNumberValue { number }
            ... on ProjectV2ItemFieldDateValue { date }
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


def fetch_issue_field(
    repo: str,
    issue_number: int,
    project_number: str,
    field: str = "Tier",
    run=None,
    ttl: float = DEFAULT_TTL_SECONDS,
    cache_dir: Path = None,
) -> str | None:
    """**The documented answer to "what is field X on issue N"** (AC4).

    About one GraphQL complexity point whatever the board's size, against
    hundreds for `fetch_full_board()`. Generalized from Tier-only in
    harmonic-forge#468 — `Sequence`, `Theme`, `Venture` and any other field are
    now as cheap to ask about as `Tier` was, which matters because the question
    that zeroed the quota on 2026-09-04 was about `Sequence` and had no cheap
    answer available.

    `ttl` defaults to a real window (AC1). Pass `ttl=0` for verify-after-write.

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
        _issue_cache_file(repo, issue_number, project_number, resolved_cache_dir, field)
        if ttl > 0 else None
    )
    if cache_file is not None:
        try:
            if cache_file.exists() and (time.time() - cache_file.stat().st_mtime) < ttl:
                with open(cache_file) as f:
                    return json.load(f).get("value")
        except (OSError, json.JSONDecodeError, AttributeError):
            pass  # unreadable cache is a miss, never an error

    value = _fetch_issue_field_live(repo, issue_number, project_number, field, run)

    # Written only on a successful read. A GhItemListError propagates out of
    # the call above without touching the cache, so a transient failure is
    # never frozen into a TTL window of false "unset".
    if cache_file is not None:
        try:
            resolved_cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = cache_file.with_suffix(".json.tmp")
            with open(tmp, "w") as f:
                json.dump({"value": value}, f)
            tmp.replace(cache_file)  # atomic: a concurrent hook never reads a half-written file
        except OSError:
            pass  # an unwritable cache degrades to "uncached", never to an error
    return value


def fetch_issue_tier(
    repo: str,
    issue_number: int,
    project_number: str,
    run=None,
    ttl: float = 0,
    cache_dir: Path = None,
) -> str | None:
    """One issue's Tier (harmonic-forge#257). Thin wrapper over
    `fetch_issue_field`.

    Kept because the model-tier gate and `l1_post.py` both call it by name on a
    hot path, and because `ttl=0` is genuinely right *here*: the gate reads a
    tier to decide whether to block a tool call, and a cached "no tier" from
    before the operator set one would gate wrongly for the whole window. The
    inverted default belongs on the general read, not on this one.
    """
    value = fetch_issue_field(
        repo, issue_number, project_number, field="Tier",
        run=run, ttl=ttl, cache_dir=cache_dir,
    )
    # `.strip().lower()` is TIER-specific and stays here rather than moving into
    # the general read. The tier vocabulary is lowercase and every caller
    # compares against lowercase constants — but `Sequence` is a number and a
    # Text field's casing is the operator's, so lowercasing in
    # `fetch_issue_field` would corrupt every other field to fix this one.
    # Generalizing dropped this on the first pass and `test_tier_is_normalised`
    # caught it.
    return value.strip().lower() if isinstance(value, str) and value.strip() else None


def _fetch_issue_field_live(
    repo: str,
    issue_number: int,
    project_number: str,
    field: str = "Tier",
    run=None,
) -> str | None:
    """The uncached targeted read. Split out so the caching wrapper has exactly
    one place to write the cache, rather than one per return path.

    Returns the value as a string whatever the field's type — the callers that
    exist compare against string constants, and a `Sequence` of `12` is more
    useful as `"12"` than as a float that has to be re-formatted at every use.
    `None` means "no value on this board", which is a real answer.
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
        "-f", f"query={_FIELD_QUERY}",
        "-F", f"owner={owner}",
        "-F", f"repo={name}",
        "-F", f"number={int(issue_number)}",
        "-F", f"field={field}",
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
        value = node.get("value") or {}
        # One of `name` (single-select), `text`, `number`, `date` — whichever
        # fragment matched. A field with no value on this item yields {}.
        for key in ("name", "text", "number", "date"):
            if key in value and value[key] is not None:
                raw = value[key]
                # A number field comes back as a float; `12.0` is not a useful
                # answer to "what is Sequence on issue N".
                if isinstance(raw, float) and raw.is_integer():
                    return str(int(raw))
                return str(raw)
        return None
    return None


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
