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

    l1_post.py            (HRSE2)         -- handoff/ready-for-l3/sweep/ae/
                                              ae-and-sweep/rework, posted_by="l1"
    l2_post.py             (this repo)    -- plan/completion/blocked/finding,
                                              posted_by="l2"
    post_lane_discussion.py (HRSE2)       -- discussion/plan/spec/gate-result,
                                              posted_by=f"l{LANE}" or "unknown"

`watch_lane_posts.py` (this repo) is the one reader: `read_candidates`.

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
`read_candidates` take an explicit `base_dir` -- no test needs to
monkeypatch a private module constant to stay off the real
`~/.claude/state/belt/candidates/` directory. `test_belt_candidates.py`'s
`BeltCandidatesRealDirUntouchedTests` asserts, for this module's own test
run, that the real directory's on-disk state is byte-identical before and
after -- so a future test that forgets to pass `base_dir` fails loudly
instead of quietly writing into the operator's live belt state.
"""
from __future__ import annotations

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
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(entry), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        print(f"[belt-candidates] record failed (non-fatal): {exc}",
              file=sys.stderr)


def _parse_iso(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def read_candidates(
    repos: Iterable[str],
    lane: str,
    *,
    queue_kinds: dict[str, tuple[str, ...]],
    queue_posters: dict[str, tuple[str, ...]],
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
    notably -- see `BeltCandidatesRealDirUntouchedTests`) must never have
    it happen as a side effect of merely reading. `watch_lane_posts.py`'s
    own `read_queue_candidates` wrapper, the one caller that runs against
    the real directory in production, opts in explicitly."""
    base = base_dir or DEFAULT_CANDIDATES_DIR
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=max_age_days)
    wanted_repos = set(repos)
    kinds = set(queue_kinds.get(lane, ()))
    posters = set(queue_posters.get(lane, ()))
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
                try:
                    path.unlink()
                except OSError:
                    pass
            continue
        if repo not in wanted_repos:
            continue
        if kind not in kinds or posted_by not in posters:
            continue
        candidates.add((repo, issue))
    return candidates
