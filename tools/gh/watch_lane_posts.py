#!/usr/bin/env python3
"""Poll GitHub issue comments for a lane-post signal, one event per line
(harmonic-forge#442).

**Only Lane 1's posting tool (`l1_post.py` / `mise run l1-post`) stamps a
machine-readable marker** -- confirmed live against hrse#1530's real
comment history, 2026-09-03:

    <!-- l1-post v1; kind=handoff; posted-by=LANE-unset -->
    <!-- l1-post v1; kind=discussion; posted-by=LANE1 -->
    <!-- l1-post v1; kind=ready-for-l3; sha=...; body-sha256=...; checks=... -->

`l2_post.py`'s receipt-backed status comments and Lane 3's spec/gate
comments carry **no such marker** -- verified by grepping full comment
bodies, not assumed. They are plain markdown, distinguishable only by
their first heading line, which is NOT a fixed short code either (Lane 3's
real heading was `## Lane 3 Test Spec`, not `## L3S`). So detection here
is two different mechanisms depending on which lane posted:

- Lane 1 -> the `<!-- l1-post v1; kind=X -->` marker (reliable, exact).
- Lane 2 -> first line matches `^## L2[A-Z]` (`L2P`/`L2D`/`L2B` observed).
- Lane 3 -> first line matches `^## L3\\b` or `^## Lane 3\\b` (heuristic --
  no fixed vocabulary confirmed; widen this pattern if a real Lane 3
  heading is seen that doesn't match).

This is the correct signal for "did another lane just do something I need
to react to". Local git state (a shared worktree's `git log`) is NOT that
signal by itself -- a lane frequently works in a disposable per-issue
worktree (`/tmp/hrse2-<N>-impl`) and never pushes until its counterpart
reviews, so a bare commit-watch can sit silent through a real completion
(this happened live on hrse#1530 -- the trigger for this script).

**But the worktree IS the right way to discover WHICH repo/issue to poll**
(operator's own design, corrected 2026-09-03 after a first draft required
manually plugging in `--repo`/`--issues` every session): a lane's current
branch names the issue it is on right now, so re-deriving `(repo, issue)`
from a worktree's live branch every poll cycle means the same command works
unmodified across every issue a lane ever picks up -- no manual input, and
it follows the lane automatically when it checks out a new branch.

Branch-name convention observed live across this repo's worktrees:
`l2/h1530-...`, `h1522/tier-group-rename`, `fix/1498-...`, `l2/f433-...`
(the `f` prefix means the branch's *subject issue* lives in
harmonic-forge, even though the worktree hosting it is a hrse checkout --
a real case, not hypothetical: a Tooling Exception can touch shared
`harmonic-forge/tools/` from an hrse-repo branch). A single run of 2-6
digits, optionally prefixed with one of `h`/`f`/`i` (hrse / harmonic-forge
/ cymagraph-infra), bounded by `/`, `-`, or the string's start/end, is
read as the issue number; the prefix letter picks the repo, and an
unprefixed number falls back to the worktree's own `git remote` repo.
Branches with no such run (`docs/some-name`) are silently skipped.

Designed to be pasted as a `Monitor` tool `command` verbatim, or run
standalone from a terminal. Prints ONE line per new comment whose detected
`lane` (`l1`/`l2`/`l3`) is in `--watch`; every other comment (plain chat,
a lane not being watched) is silent.

**A worktree only tells you about ONE issue.** Lane 3 has no single
worktree the way Lane 2 does -- it needs to find WHICHEVER issue is
currently queued to it, repo-wide, without anyone naming a number.
`--queue-for l3` answers that: it searches the repo for open issues
carrying an `l1-post` marker whose `kind` is `ready-for-l3`, `ae`, or
`sweep` (the three kinds that hand Lane 3 something to do), via
`gh search issues ... "l1-post v1; kind=<kind>"` -- a literal-substring
search, not a keyword match, so it doesn't pick up unrelated mentions of
the word (verified live 2026-09-03: zero false positives across all three
kinds on this repo's real history). A search hit only means the marker
exists SOMEWHERE on the issue, so each candidate's full comment history is
then re-checked: an issue only counts as currently queued if that marker
is still the LATEST classified comment -- once Lane 3 (or anyone) posts
anything after it, the issue drops out of the queue on its own, with no
separate "I'm done" bookkeeping required anywhere.

Usage
-----
    # The Lane 3 case: find whatever is queued to me, repo-wide, no
    # worktree and no issue number needed:
    python3 watch_lane_posts.py --queue-for l3 --repo vitalharmony/hrse \\
        --interval 30

    # Self-discovering from a worktree (the Lane 2 case): watch whatever
    # issue THIS worktree's current branch is on, re-derived every cycle.
    # Run from inside the worktree, or pass its path explicitly:
    python3 watch_lane_posts.py --worktrees . --watch l1 --interval 30
    python3 watch_lane_posts.py --worktrees ~/Harmonic_Projects/HRSE2-lane2 \\
        ~/Harmonic_Projects/HRSE2-lane3 --watch l2 --watch l3

    # Manual override, when there is no worktree to read (or watching an
    # issue this session isn't actually checked out on):
    python3 watch_lane_posts.py --repo vitalharmony/hrse --issues 1530 \\
        --watch l2 --watch l3 --interval 30

`--queue-for`, `--worktrees`, and `--repo`/`--issues` may all be combined;
the watched set is their union, re-derived every cycle for `--queue-for`
and `--worktrees` alike. Exits only on error or Ctrl-C; runs until stopped
otherwise.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import time

_L1_MARKER_RE = re.compile(r"<!--\s*l1-post\s+v\d+;\s*kind=([\w-]+)")
_L2_HEADING_RE = re.compile(r"^##\s+L2[A-Z]\b")
_L3_HEADING_RE = re.compile(r"^##\s+(L3\b|Lane 3\b)")

#: A run of 2-6 digits, optionally prefixed with one repo-selecting letter,
#: bounded by `/`, `-`, or the string's start/end -- e.g. `h1530` in
#: `l2/h1530-null-tolerant-sync-predicate`, `1498` in `fix/1498-...`,
#: `f433` in `l2/f433-drift-check-patch-id`. `re.search`, not `match` --
#: the run can sit anywhere in the branch name.
_BRANCH_ISSUE_RE = re.compile(r"(?:^|/)(?P<prefix>[hHfFiI])?(?P<num>\d{2,6})(?=[-/]|$)")

#: Prefix letter -> repo, for a branch whose subject issue lives in a
#: DIFFERENT repo than the worktree hosting it (a Tooling Exception can
#: touch shared harmonic-forge/tools/ from an hrse-repo branch).
_PREFIX_REPO = {
    "h": "vitalharmony/hrse",
    "f": "vitalharmony/harmonic-forge",
    "i": "vitalharmony/cymagraph-infra",
}

_REMOTE_REPO_RE = re.compile(r"github\.com[:/](?P<repo>[\w.-]+/[\w.-]+?)(?:\.git)?$")

#: `l1-post` kinds that hand each lane something to do. Only `l3` is
#: implemented for `--queue-for` today (the concrete, live need); `l2`'s
#: kinds are listed for completeness and future use, but `l2` almost
#: always has its own worktree to self-discover from instead (see
#: `discover_from_worktree`), so the queue path matters less there.
QUEUE_KINDS = {
    "l3": ("ready-for-l3", "ae", "sweep"),
    "l2": ("handoff", "discussion"),
}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_git(worktree: str, *args: str) -> str | None:
    result = subprocess.run(["git", "-C", worktree, *args],
                            capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _worktree_repo(worktree: str) -> str | None:
    url = _run_git(worktree, "remote", "get-url", "origin")
    if not url:
        return None
    match = _REMOTE_REPO_RE.search(url)
    return match.group("repo") if match else None


def _worktree_branch(worktree: str) -> str | None:
    branch = _run_git(worktree, "branch", "--show-current")
    if branch:
        return branch
    # Detached HEAD (a review worktree checked out at a bare SHA/branch
    # ref rather than a local branch) -- fall back to whatever ref name
    # is available, which still carries the issue number in its path.
    return _run_git(worktree, "rev-parse", "--abbrev-ref", "HEAD")


def discover_from_worktree(worktree: str) -> tuple[str, int] | None:
    """`(repo, issue)` from a worktree's current branch, or `None` if the
    path isn't a git worktree or its branch names no issue."""
    branch = _worktree_branch(worktree)
    if not branch or branch == "HEAD":
        return None
    match = _BRANCH_ISSUE_RE.search(branch)
    if not match:
        return None
    prefix = (match.group("prefix") or "").lower()
    issue = int(match.group("num"))
    if prefix:
        return _PREFIX_REPO[prefix], issue
    repo = _worktree_repo(worktree)
    return (repo, issue) if repo else None


def _fetch_comments(repo: str, issue: int, since: str) -> list[dict]:
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/issues/{issue}/comments?since={since}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"[watch_lane_posts] gh api failed for #{issue}: "
              f"{result.stderr.strip()}", file=sys.stderr)
        return []
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return []


def _classify(body: str) -> tuple[str, str] | None:
    """Returns `(lane, detail)` -- `detail` is the `kind=` value for l1-post,
    or the matched heading text for l2/l3 -- or `None` if unclassifiable."""
    marker = _L1_MARKER_RE.search(body)
    if marker:
        return "l1", marker.group(1)
    headline = body.strip().split("\n", 1)[0]
    if _L2_HEADING_RE.match(headline):
        return "l2", headline
    if _L3_HEADING_RE.match(headline):
        return "l3", headline
    return None


def _search_candidates(repo: str, marker_text: str) -> set[int]:
    """Open issues whose comment history contains `marker_text` SOMEWHERE --
    a coarse, cheap pre-filter. `discover_queue` re-checks each one to see
    if that marker is still the LATEST classified comment."""
    result = subprocess.run(
        ["gh", "search", "issues", "--repo", repo, "--state", "open",
         marker_text, "--json", "number"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"[watch_lane_posts] gh search failed: {result.stderr.strip()}",
              file=sys.stderr)
        return set()
    try:
        return {row["number"] for row in json.loads(result.stdout)}
    except json.JSONDecodeError:
        return set()


def _fetch_all_comments(repo: str, issue: int) -> list[dict]:
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/issues/{issue}/comments"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return []
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return []


def discover_queue(repo: str, lane: str) -> dict[int, str]:
    """`{issue: kind}` for every open issue currently queued to `lane` --
    an l1-post marker whose kind is one of `QUEUE_KINDS[lane]` is the
    LATEST classified comment on that issue. Self-clearing: once anything
    is posted after that marker, the issue drops out on its own, so there
    is no separate "done" bookkeeping anywhere."""
    kinds = QUEUE_KINDS[lane]
    candidates: set[int] = set()
    for kind in kinds:
        candidates |= _search_candidates(repo, f"l1-post v1; kind={kind}")

    queued: dict[int, str] = {}
    for issue in candidates:
        last_kind: tuple[str, str] | None = None
        for comment in _fetch_all_comments(repo, issue):
            classified = _classify(comment.get("body", ""))
            if classified is not None:
                last_kind = classified
        if last_kind and last_kind[0] == "l1" and last_kind[1] in kinds:
            queued[issue] = last_kind[1]
    return queued


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--worktrees", nargs="+", default=[],
                        help="worktree path(s) -- (repo, issue) re-derived from each one's "
                             "CURRENT branch every poll cycle, so this follows a lane across "
                             "issues with zero reconfiguration")
    parser.add_argument("--repo", help="owner/repo for a manual --issues override")
    parser.add_argument("--issues", type=int, nargs="+", default=[],
                        help="issue numbers to poll, paired with --repo (static, not "
                             "re-derived) -- for watching an issue with no worktree")
    parser.add_argument("--queue-for", choices=sorted(QUEUE_KINDS), default=None,
                        help="repo-wide: find ANY open issue currently queued to this lane "
                             "(paired with --repo), no worktree or issue number needed")
    parser.add_argument("--watch", action="append", default=[],
                        choices=["l1", "l2", "l3"],
                        help="lane whose posts to surface on watched issues -- l1, l2, "
                             "and/or l3 (repeatable). Not required when only --queue-for "
                             "is used -- queue entry/exit is its own event.")
    parser.add_argument("--interval", type=int, default=30, help="poll interval, seconds")
    args = parser.parse_args()

    if args.issues and not args.repo:
        parser.error("--issues requires --repo")
    if args.queue_for and not args.repo:
        parser.error("--queue-for requires --repo")
    if not args.worktrees and not args.issues and not args.queue_for:
        parser.error("give at least one of --worktrees, --repo/--issues, or --queue-for")
    if not args.watch and not args.queue_for:
        parser.error("--watch is required unless --queue-for is given")

    watch = set(args.watch)
    static_pairs = {(args.repo, n) for n in args.issues} if args.repo else set()
    since = _now()
    print(f"[watch_lane_posts] worktrees={args.worktrees or None} "
          f"static={sorted(static_pairs) or None} queue_for={args.queue_for or None} "
          f"lanes={sorted(watch) or None} every {args.interval}s", file=sys.stderr)

    last_discovered: set[tuple[str, int]] = set()
    last_queue: dict[int, str] = {}
    while True:
        time.sleep(args.interval)
        now = _now()

        discovered = {pair for path in args.worktrees
                      if (pair := discover_from_worktree(path)) is not None}
        if discovered != last_discovered:
            print(f"[watch_lane_posts] now watching {sorted(discovered | static_pairs)}",
                  file=sys.stderr)
            last_discovered = discovered

        if args.queue_for:
            queue = discover_queue(args.repo, args.queue_for)
            for issue, kind in queue.items():
                if last_queue.get(issue) != kind:
                    print(f"{args.repo}#{issue} queued-for-{args.queue_for} kind={kind}")
                    sys.stdout.flush()
            for issue in set(last_queue) - set(queue):
                print(f"{args.repo}#{issue} left-queue-for-{args.queue_for}")
                sys.stdout.flush()
            last_queue = queue

        for repo, issue in discovered | static_pairs:
            for comment in _fetch_comments(repo, issue, since):
                classified = _classify(comment.get("body", ""))
                if classified is None:
                    continue
                lane, detail = classified
                if lane not in watch:
                    continue
                print(f"{repo}#{issue} {lane} — {detail}")
                sys.stdout.flush()
        since = now


if __name__ == "__main__":
    raise SystemExit(main())
