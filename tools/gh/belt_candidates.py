#!/usr/bin/env python3
"""Shared belt-candidate recorder/reader (harmonic-forge#691, rescoped).

Replaces the account-wide `search/issues` scan harmonic-forge#686 removed:
a fresh Lane 1 handoff/rework/ready-for-l3/ae/sweep, a fresh Lane 2 `plan`,
or a fresh Lane 3 spec/gate-result may land on an issue with **no worktree
yet** -- the worktree (or the belt's other candidate sources) only exists
in response to the post. `--all-worktrees`/`--issues` alone cannot see that
class of inbound, which is exactly the gap harmonic-forge#596/#618 already
fixed once and which removing the scan without a replacement reopened.

**One module, three writers, one reader.** Every place any `l1-post v1`
marker is ever written calls `record_candidate` on every successful post:

    l1_post.py            (this repo)     -- handoff/ready-for-l3/sweep/ae/
                                              ae-and-sweep/rework, posted_by="l1"
    l2_post.py             (this repo)    -- plan/completion/blocked/finding,
                                              posted_by="l2"
    post_lane_discussion.py (this repo)   -- discussion/plan/spec/gate-result,
                                              posted_by=f"l{LANE}" or "unknown"

`watch_lane_posts.py` (this repo) is the one reader: `read_candidates`.

**Every mutation of the store happens under `_store_lock`** (harmonic-forge
#854). The sites are three: `record_candidate`'s write, `retire_candidate`'s
read-and-rewrite, and `_prune_if_stale`'s archive-and-unlink. A new mutation
site must take the lock too and join this list.

**Kind- and poster-aware, not just repo/age (the pre-rescope defect).** The
original design recorded only `{repo, issue, posted_at}` -- every issue
*any* writer ever touched stayed a candidate for every lane for 14 days,
which measured out to roughly 200 REST calls/tick against a ~17.6/tick
baseline (`discover_queue` re-checks each candidate's full comment history
live). Recording `kind` and `posted_by` lets the reader ask the same
question `discover_queue` itself asks -- is the newest recorded post one
this LANE owes work on, from a poster this lane accepts from -- *before*
spending a single REST call, collapsing the candidate set back down to
single digits per tick.

**One file per issue, not one growing JSONL (AC3').** `candidates/
<owner>__<repo>__<issue>.json`, written with a temp-file-then-`os.replace`
so a reader never observes a partial write. Bounded by open-issue count,
not post count: a hot issue that gets ten posts still occupies one file,
overwritten each time with only the newest entry -- which is all a
"queue-eligibility" pre-filter ever needs, since `discover_queue`'s own
re-check is what actually decides membership. Pruning a stale entry is
therefore a safe, isolated `unlink` of that one issue's file -- never a
rewrite that risks truncating a different issue's concurrent write, which
is what made the JSONL shape reader-prune-unsafe in the first place (two
independent writer processes, in two different repos, appending with no
lock).

**No GitHub call on the read side**, still. `discover_queue`'s own
per-issue re-check against the live comment thread is what actually
decides queue membership and kind, exactly as it does for a worktree- or
`--issues`-derived candidate -- this module only narrows which issues are
worth asking about.

**Injectable path, everywhere (AC4').** Both `record_candidate` and
`read_candidates` take an explicit `base_dir`, and every test in this
module's own `test_belt_candidates.py` passes one. That alone does not
cover every *caller* of this module, though -- all three writers' end-to-end
tests can call the recorder without a `base_dir`. `tools/run_tests.py`'s
`redirected_belt_candidates_dir` context manager
monkeypatches `DEFAULT_CANDIDATES_DIR` to a per-run tmp dir around the one
`unittest.TextTestRunner` invocation that `mise run check`/CI actually
call -- so a future test that forgets to pass `base_dir` writes to a
scratch dir instead of the operator's real state. F706's focused
`run_lane1_transport_tests.py` uses the same guard for the standalone task.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

UTC = timezone.utc

#: Real, production location -- the same directory the pre-rescope design
#: used for its single JSONL file. Every writer and the reader default
#: here; every test passes `base_dir` instead (AC4').
DEFAULT_CANDIDATES_DIR = Path.home() / ".claude" / "state" / "belt" / "candidates"

#: Same value the pre-rescope design used, preserved rather than
#: re-litigated -- 14 days safely spans a long weekend without an entry
#: aging out from under a lane that was simply offline.
DEFAULT_MAX_AGE_DAYS = 14

#: harmonic-forge#854 preclose: a closed-marked entry is skipped for this long,
#: then offered again so `discover_queue` re-reads the issue's state. An issue
#: reopened with no new post is queued again within this window; one still
#: closed is re-marked (the stamp refreshed), so a closed issue costs at most
#: one issue read per window.
CLOSED_RECHECK = timedelta(hours=1)


def _candidate_path(base_dir: Path, repo: str, issue: int) -> Path:
    """`candidates/<owner>__<repo>__<issue>.json` (AC3'). `/` cannot survive
    into a filename, so it is replaced rather than left to fail `mkdir`."""
    owner, _, name = repo.partition("/")
    if not name:
        owner, name = "unknown", repo
    safe_owner = owner.replace("/", "_") or "unknown"
    safe_name = name.replace("/", "_") or "unknown"
    return base_dir / f"{safe_owner}__{safe_name}__{issue}.json"


def record_candidate(
    repo: str,
    issue: int,
    kind: str,
    posted_by: str,
    *,
    base_dir: Path | None = None,
) -> None:
    """Record that `repo`#`issue` just received an `l1-post v1` marker of
    `kind`, posted by lane `posted_by` (`"l1"`/`"l2"`/`"l3"`, or `"unknown"`
    when the posting session's `LANE` could not be determined).

    Best-effort and non-raising: a write failure here must never fail the
    post itself -- the comment is already on GitHub by the time any writer
    calls this. Atomic: a temp file in the same directory, then
    `os.replace`, so a reader never observes a partially written file
    (two writer processes never touch the same issue's file at the same
    instant in practice, but a crash mid-write must still never corrupt
    it)."""
    base = base_dir or DEFAULT_CANDIDATES_DIR
    entry = {
        "repo": repo,
        "issue": issue,
        "kind": kind,
        "posted_by": posted_by,
        "posted_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    path = _candidate_path(base, repo, issue)
    try:
        base.mkdir(parents=True, exist_ok=True)
        with _store_lock(base):
            _replace(path, entry)
    except OSError as exc:
        print(f"[belt-candidates] record failed (non-fatal): {exc}",
              file=sys.stderr)


@contextlib.contextmanager
def _store_lock(base: Path):
    """An exclusive lock over the candidate store's writers (harmonic-forge#854
    preclose). `retire_candidate` reads an entry and rewrites it; holding this
    lock across both steps, and across every `record_candidate` write, means a
    fresh post can never land between them and be overwritten."""
    base.mkdir(parents=True, exist_ok=True)
    with open(base / ".lock", "a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _replace(path: Path, entry: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(entry), encoding="utf-8")
    os.replace(tmp, path)


def _parse_iso(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def read_candidates(
    repos: Iterable[str],
    lane: str,
    *,
    queue_kinds: dict[str, tuple[str, ...]],
    queue_posters: dict[str, tuple[str, ...]],
    any_poster_kinds: dict[str, tuple[str, ...]] | None = None,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    now: datetime | None = None,
    base_dir: Path | None = None,
    prune: bool = False,
) -> set[tuple[str, int]]:
    """`(repo, issue)` pairs recorded recently whose newest entry is
    queue-eligible FOR `lane` (AC2') -- `kind` is one of `queue_kinds[lane]`
    and `posted_by` is one of `queue_posters[lane]`, mirroring exactly the
    question `discover_queue` itself asks once it has the candidate in
    hand. This is the pre-filter that keeps the candidate set at single
    digits instead of "every issue any writer touched in 14 days" -- the
    kind-less design's measured ~200-calls/tick risk against the ~17.6/tick
    baseline.

    Filtered to `repos` and to entries no older than `max_age_days`.
    Malformed or unreadable entries (a partial write caught mid-replace, a
    hand-edited file, an unknown lane in `queue_kinds`/`queue_posters`) are
    skipped rather than raising -- one bad file must not blind the belt to
    every real candidate beside it, same principle `discover_queue` already
    applies to one issue's failed comment fetch.

    `prune=True` unlinks a stale entry's file as it is read -- safe because
    it is a targeted `unlink` of that one issue's own file, never a rewrite
    of a shared structure a concurrent writer could be appending to (AC3').
    Defaults to `False`: pruning is a real filesystem mutation, and a
    caller that does not explicitly opt in (every test in this repo,
    notably -- see `tools/run_tests.py`'s `redirected_belt_candidates_dir`
    for how the suite keeps itself off the real directory in the first
    place) must never have it happen as a side effect of merely reading.
    `watch_lane_posts.py`'s
    own `read_queue_candidates` wrapper, the one caller that runs against
    the real directory in production, opts in explicitly."""
    base = base_dir or DEFAULT_CANDIDATES_DIR
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=max_age_days)
    wanted_repos = set(repos)
    kinds = set(queue_kinds.get(lane, ()))
    posters = set(queue_posters.get(lane, ()))
    #: harmonic-forge#851: kinds that queue to `lane` from any poster (a
    #: FAIL gate result to Lane 2), mirroring `discover_queue`'s exception.
    any_poster = set((any_poster_kinds or {}).get(lane, ()))
    candidates: set[tuple[str, int]] = set()
    try:
        paths = sorted(base.glob("*.json"))
    except OSError:
        return candidates
    for path in paths:
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            repo = entry["repo"]
            issue = int(entry["issue"])
            kind = entry["kind"]
            posted_by = entry["posted_by"]
            posted_at = _parse_iso(entry["posted_at"])
        except (json.JSONDecodeError, KeyError, ValueError, TypeError, OSError):
            continue
        if posted_at < cutoff:
            if prune:
                _prune_if_stale(base, path, cutoff)
            continue
        if _recently_closed(entry, now):
            # harmonic-forge#854: retired by `retire_candidate` because the
            # issue was closed. Skipped for CLOSED_RECHECK, then offered again
            # so its state is re-read (a reopen with no new post is not lost);
            # a fresh `record_candidate` clears the mark at once.
            continue
        if repo not in wanted_repos:
            continue
        if kind not in kinds or (posted_by not in posters and kind not in any_poster):
            continue
        candidates.add((repo, issue))
    return candidates


def _recently_closed(entry: dict, now: datetime) -> bool:
    stamp = entry.get("closed_at")
    if not stamp:
        return False
    try:
        return now - _parse_iso(stamp) < CLOSED_RECHECK
    except (ValueError, TypeError):
        return False  # an unreadable mark is not a mark: offer the pair


def _prune_if_stale(base: Path, path: Path, cutoff: datetime) -> None:
    """Archive then unlink one aged-out entry, under the store lock
    (harmonic-forge#854 preclose). The entry is re-read under the lock and kept
    when a `record_candidate` refreshed it since the unlocked read. The lock is
    taken per path, never around `read_candidates`' loop: `flock` locks belong
    to the open file description, so a nested `_store_lock` in one process
    blocks on itself."""
    try:
        with _store_lock(base):
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
                if _parse_iso(entry["posted_at"]) >= cutoff:
                    return
            except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError):
                return
            # harmonic-forge#826: this file is the only on-disk record of the
            # post's kind/poster/time, so it is archived first and unlinked
            # only when the archive holds it.
            if _archive_candidate(path, entry) == 1:
                path.unlink()
    except OSError:
        pass


def closed_marked(repo: str, issue: int, *, base_dir: Path | None = None,
                  now: datetime | None = None) -> bool:
    """Whether `repo`#`issue`'s entry carries a live closed mark (harmonic-forge
    #854 post-verdict). `queue_cycle` drops a carried-forward queue entry for a
    marked pair: the mark is why the pair left the candidate set, so its absence
    from this cycle's check is not "never looked at"."""
    path = _candidate_path(base_dir or DEFAULT_CANDIDATES_DIR, repo, issue)
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(entry, dict) and _recently_closed(entry, now or datetime.now(UTC))


def _archive_candidate(path: Path, entry: dict) -> int:
    """Archive one aged-out candidate file by its repo; 0 means keep it."""
    try:
        telemetry = str(Path(__file__).resolve().parent.parent / "telemetry")
        if telemetry not in sys.path:
            sys.path.insert(0, telemetry)
        import archive  # noqa: PLC0415
        return archive.archive("belt-candidates", [{"path": path.name, **entry}],
                               origin=archive.origin_for_repo(entry.get("repo")))
    except Exception:
        return 0


def retire_candidate(repo: str, issue: int, *, read_before: datetime,
                     base_dir: Path | None = None, now: datetime | None = None) -> bool:
    """Mark a CLOSED issue's candidate entry closed, in place (harmonic-forge#854).

    The entry gains (or refreshes) a `closed_at` stamp; nothing is archived or
    unlinked here. `read_candidates` skips a marked entry for CLOSED_RECHECK,
    then offers it again so its state is re-read, and a later
    `record_candidate` writes a whole fresh entry without the mark. The read
    and the rewrite happen under the store lock, so a concurrent post cannot be
    overwritten. An entry posted at or after `read_before` (the moment the
    cycle read the store, compared to the second as `posted_at` is stored) is
    left unmarked. Archive-then-unlink stays solely the 14-day age prune.
    Returns whether it marked."""
    base = base_dir or DEFAULT_CANDIDATES_DIR
    path = _candidate_path(base, repo, issue)
    try:
        with _store_lock(base):
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
                posted_at = _parse_iso(entry["posted_at"])
            except FileNotFoundError:
                return False  # a worktree- or --issues-derived candidate has no entry
            except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
                _retire_failed(repo, issue, exc)
                return False
            if posted_at >= read_before.replace(microsecond=0):
                return False
            entry["closed_at"] = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
            _replace(path, entry)
    except OSError as exc:
        _retire_failed(repo, issue, exc)
        return False
    return True


def _retire_failed(repo: str, issue: int, exc: Exception) -> None:
    """One line per failed mark, so a store that is not draining is never
    silent (harmonic-forge#854 sticky-wicket ruling)."""
    print(f"[belt-candidates] could not mark closed {repo}#{issue} (non-fatal): {exc}",
          file=sys.stderr)

