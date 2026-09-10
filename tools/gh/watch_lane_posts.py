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
- Lane 2 -> first line matches `^## L2[A-Z]` (`L2P`/`L2D`/`L2B` observed) or
  `^## L2 Finding` (harmonic-forge#571's `--kind finding` -- deliberately
  spelled with a space so it is never mistaken for an `L2P`/`L2D`/`L2B`
  status transition by this classifier or by HRSE2's `lane_state.py`, while
  still being visible to this belt as an `l2` event).
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
carrying an `l1-post` marker whose `kind` is one of `QUEUE_KINDS["l3"]`
(`ready-for-l3`, `ae`, `sweep`, or `ae-and-sweep` -- the kinds that hand
Lane 3 something to do, read from the constant directly rather than
restated here, since restating it is exactly what let this prose drift
out of sync with the code once already, harmonic-forge#579), via
`gh search issues ... "l1-post v1; kind=<kind>"` -- a literal-substring
search, not a keyword match, so it doesn't pick up unrelated mentions of
the word (verified live 2026-09-03: zero false positives across all
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
from pathlib import Path
from typing import Iterable, Mapping

sys.path.insert(0, str(Path(__file__).parent))

from belt_mechanics import (  # noqa: E402
    CallCounter,
    IdentityMismatch,
    assert_identity,
    gh_as,
)

#: harmonic-forge#518 AC4. Every GitHub call in this file routes through
#: `gh-as <account>`, never bare `gh`. Bare `gh` resolves against whatever the
#: global config points at, so a `vitalharmony`-scoped session polling a
#: `harmonicarchitect` repo 404s or returns empty — and an empty monitor result
#: is indistinguishable from "no new work." Silence is the one signal this
#: protocol cannot interpret, so the account is explicit.
#:
#: Module-level because the call sites are leaf helpers reached from several
#: paths; threading it through every signature would be a larger diff than the
#: change warrants and would not make it more explicit.
_ACCOUNT = "vitalharmony"
_COUNTER = CallCounter()

#: The full marker text, not just its `kind=` field (harmonic-forge#583) --
#: `posted-by` has to be extracted from the SAME matched span, the same way
#: `fetch_lane1_context.py`'s `is_lane1_comment` already scopes its field
#: regexes to one matched marker rather than `.search()`-ing the whole body
#: (harmonic-forge#269): scoping only the kind lookup and leaving
#: `posted-by` unscoped would reopen exactly that hole one field over.
_MARKER_RE = re.compile(r"<!--\s*l1-post\s+v\d+;.*?-->", re.DOTALL)
_KIND_RE = re.compile(r"kind=([\w-]+)")
_POSTED_BY_RE = re.compile(r"posted-by=([\w-]+)")
#: harmonic-forge#583 AC1/AC2. `l2_post.py` now stamps this same marker on
#: every kind it posts -- so the marker's mere presence no longer implies
#: Lane 1 the way it safely could before. `posted-by`, when present, says
#: which lane actually posted; ABSENT is the backward-compatibility case
#: (every marker minted before this landed, Lane 1's entire historical
#: corpus, carries no `posted-by` at all) and an unrecognized value both
#: default to `l1` -- do not raise or treat either as unclassifiable, that
#: default IS the contract that keeps the pre-#583 corpus reading the same.
_POSTED_BY_LANE = {"LANE2": "l2", "LANE3": "l3"}
#: `L2[A-Z]` catches the status-transition headings (`L2S`/`L2D`/`L2B`).
#: `L2 Finding` (harmonic-forge#571) is a distinct alternative, not a widened
#: character class, precisely so it stays visible to this belt without ever
#: being confused for a status transition by anything reading `L2[A-Z]`.
#: Still matches a legacy `## L2P` comment from before the harmonic-forge#583
#: rename (AC8) -- `L2[A-Z]` never depended on which letter, and no
#: historical body is rewritten.
_L2_HEADING_RE = re.compile(r"^##\s+(?:L2[A-Z]\b|L2 Finding\b)")
_L3_HEADING_RE = re.compile(r"^##\s+(L3\b|Lane 3\b)")
#: The `L2 Finding` heading specifically, as opposed to a real `L2[A-Z]`
#: status transition -- both are matched by `_L2_HEADING_RE` above (a
#: finding must be visible to `_classify` at all, harmonic-forge#571 AC4),
#: but `discover_queue`'s "last classified comment wins" walk (harmonic-
#: forge#580 AC1) must not let a finding overwrite queue membership the way
#: a real status transition does: a finding is a defect report, not a lane
#: handoff, and posting one must never silently drop an issue out of
#: whichever lane's queue it already sat in.
_L2_FINDING_RE = re.compile(r"^##\s+L2 Finding\b")
#: The `## L2D` heading, for the same markerless-fallback reason as
#: `_L2_FINDING_RE` above -- harmonic-forge#583 AC4's belt-completion signal
#: needs to recognize a completion whether it classified via the marker
#: (`detail == "completion"`) or, for a comment posted before #583 landed,
#: via this heading fallback.
_L2_COMPLETION_RE = re.compile(r"^##\s+L2D\b")


def _is_l2_finding(lane: str, detail: str) -> bool:
    """True when a `_classify` result represents an `l2` `finding` --
    either the harmonic-forge#583 marker path (`detail == "finding"`) or the
    pre-#583 / markerless heading-fallback path (`detail` is the literal
    `## L2 Finding ...` headline). Both must be recognized: `l2_post.py`
    stamps a marker on every kind now (#583 AC1), so a finding posted today
    classifies via the marker branch, while the historical corpus and any
    hand-typed post still fall back to the heading (AC5)."""
    return lane == "l2" and (detail == "finding" or _L2_FINDING_RE.match(detail) is not None)


def _is_l2_completion(lane: str, detail: str) -> bool:
    """The `completion`-kind analogue of `_is_l2_finding` above, for
    harmonic-forge#583 AC4's branch-ahead-without-completion check."""
    return lane == "l2" and (detail == "completion" or _L2_COMPLETION_RE.match(detail) is not None)

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

#: `l1-post` kinds that hand each lane something to do. `l2`'s kinds are
#: `handoff` and `rework`, deliberately NOT `discussion` -- R-0337
#: (`harmonic-forge/rules/lane-shorthand.md`) measured every thread on
#: `vitalharmony/hrse` and found 63 issues whose newest marker after
#: `l2.done` was a `discussion`, and *none* of them was a request for more
#: work (closing notes, merge confirmations, gate sign-offs). A Lane 1
#: request for more work on an existing branch is posted `--kind rework`
#: specifically so it is distinguishable from that noise. `discussion` was
#: here until harmonic-forge#570's preclose review measured it live against
#: this repo (36 of 46 `--queue-for l2` hits were `discussion`, none
#: actionable) and it was removed.
QUEUE_KINDS = {
    "l3": ("ready-for-l3", "ae", "sweep", "ae-and-sweep"),
    "l2": ("handoff", "rework"),
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


def resolve_worktree(worktree: str) -> tuple[tuple[str, int] | None, str]:
    """`(pair_or_None, reason)` -- harmonic-forge#570 AC6. A worktree that
    resolves to nothing is a common, *expected* resting state (a lane between
    issues sits on a detached HEAD), and silence there reads as "no new work"
    exactly the way an empty poll result does everywhere else in this module.
    So every caller gets the reason alongside the `None`, not just the pair.
    """
    branch = _worktree_branch(worktree)
    if not branch:
        return None, "not a git worktree (no HEAD ref could be read)"
    if branch == "HEAD":
        return None, ("detached HEAD with no branch name -- name targets "
                       "explicitly instead (--repo/--issues or --queue-for)")
    match = _BRANCH_ISSUE_RE.search(branch)
    if not match:
        return None, f"branch {branch!r} names no issue number"
    prefix = (match.group("prefix") or "").lower()
    issue = int(match.group("num"))
    if prefix:
        return (_PREFIX_REPO[prefix], issue), "resolved"
    repo = _worktree_repo(worktree)
    if not repo:
        return None, (f"branch {branch!r} names issue {issue} but the "
                       "origin remote could not be resolved")
    return (repo, issue), "resolved"


def discover_from_worktree(worktree: str) -> tuple[str, int] | None:
    """`(repo, issue)` from a worktree's current branch, or `None` if the
    path isn't a git worktree or its branch names no issue."""
    pair, _reason = resolve_worktree(worktree)
    return pair


def report_resolution(worktrees: list[str]) -> list[tuple[str, tuple[str, int] | None, str]]:
    """Print, to stderr, how many of `worktrees` resolved and why any did
    not -- harmonic-forge#570 AC6. Zero resolved out of a non-empty list is
    called out explicitly rather than left to read as "nothing to do"."""
    resolutions = [(path, *resolve_worktree(path)) for path in worktrees]
    if not resolutions:
        return resolutions
    resolved = sum(1 for _, pair, _ in resolutions if pair is not None)
    print(f"[watch_lane_posts] worktree targets: {resolved}/{len(resolutions)} resolved",
          file=sys.stderr)
    for path, pair, reason in resolutions:
        if pair is None:
            print(f"[watch_lane_posts]   unresolved: {path} -- {reason}", file=sys.stderr)
    if resolved == 0:
        print(f"[watch_lane_posts] ZERO of {len(resolutions)} --worktrees target(s) "
              "resolved -- this belt is watching nothing from --worktrees. Name targets "
              "explicitly with --repo/--issues or --queue-for instead.", file=sys.stderr)
    return resolutions


def _fetch_comments(repo: str, issue: int, since: str) -> list[dict]:
    try:
        raw = gh_as(
            _ACCOUNT,
            ["api", f"repos/{repo}/issues/{issue}/comments?since={since}"],
            counter=_COUNTER,
        )
    except Exception as exc:  # noqa: BLE001 — network/auth, reported not swallowed
        print(f"[watch_lane_posts] gh api failed for #{issue}: {exc}", file=sys.stderr)
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def _classify(body: str) -> tuple[str, str] | None:
    """Returns `(lane, detail)` -- `detail` is the `kind=` value when a
    marker is present, or the matched heading text for a markerless l2/l3
    post -- or `None` if unclassifiable.

    The marker is checked before the heading (harmonic-forge#583): every
    lane's posting tool stamps one now, so it is the reliable, exact signal
    whenever present, and `posted-by` (when present) says which lane
    actually posted -- see `_POSTED_BY_LANE` above for the default that
    keeps every marker minted before this landed reading as `l1`, unchanged.
    The heading-match fallback below exists ONLY for the historical corpus
    posted before this landed (AC5) -- keep it; a comment already posted
    does not retroactively gain a marker."""
    marker_match = _MARKER_RE.search(body)
    if marker_match:
        marker = marker_match.group(0)
        kind_match = _KIND_RE.search(marker)
        if kind_match:
            kind = kind_match.group(1)
            posted_by_match = _POSTED_BY_RE.search(marker)
            posted_by = posted_by_match.group(1) if posted_by_match else None
            return _POSTED_BY_LANE.get(posted_by, "l1"), kind
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
    # harmonic-forge#518 AC16. This was `gh search issues`, which is
    # GraphQL-backed — on a polling path, against a 5,000/hour complexity-priced
    # quota shared with every concurrent lane. `search/issues` is the REST
    # equivalent and returns the identical result set (verified live: both forms
    # returned [1271, 1705] for `ready-for-l3` on vitalharmony/hrse).
    #
    # The search API carries its own rate limit (30/min authenticated), separate
    # from both core REST and GraphQL, so this does not contend with either.
    try:
        raw = gh_as(
            _ACCOUNT,
            ["api", "-X", "GET", "search/issues",
             "-f", f"q=repo:{repo} state:open {marker_text}"],
            counter=_COUNTER,
        )
    except Exception as exc:  # noqa: BLE001 — network/auth, reported not swallowed
        print(f"[watch_lane_posts] search failed: {exc}", file=sys.stderr)
        return set()
    try:
        return {row["number"] for row in json.loads(raw).get("items", [])}
    except (json.JSONDecodeError, AttributeError):
        return set()


def _fetch_all_comments(repo: str, issue: int) -> list[dict] | None:
    """Every comment on `issue`, or `None` if the fetch itself failed --
    `--paginate` is required, not optional (harmonic-forge#570 preclose
    finding): the unpaginated single-page call returns only the first 30,
    and a caller deciding "newest classified comment" off page 1 of a
    50+-comment thread silently picks the wrong one. Two real
    harmonic-forge issues already exceed 30 comments.

    `None` (harmonic-forge#579 preclose finding) is distinct from `[]`
    for the same reason `list_open_issues` distinguishes them: a caller
    that can't tell "this issue genuinely has zero comments" from "the
    call raised" cannot decide whether it's safe to conclude the issue
    carries no ball to pick up. Without this, `discover_l1_sweep`'s own
    fail-open re-check of a stale queued issue's open-state
    (`_issue_is_open`) was defeated eight lines later: the state check
    correctly kept a rate-limited issue as a candidate, but this function
    still silently returned `[]` for it, so it was excluded from `queued`
    anyway and reported as `left-queue-for-l1` -- indistinguishable from
    the issue actually having been resolved."""
    try:
        raw = gh_as(
            _ACCOUNT,
            ["api", "-X", "GET", f"repos/{repo}/issues/{issue}/comments",
             "--paginate", "-f", "per_page=100"],
            counter=_COUNTER,
        )
    except Exception as exc:  # noqa: BLE001 — reported, not swallowed (this
        # sibling of `_fetch_comments` used to swallow silently; a quota
        # exhaustion here previously read as "every issue has no comments,"
        # i.e. no work, which is exactly the failure this protocol refuses
        # everywhere else)
        print(f"[watch_lane_posts] comment fetch failed for #{issue}: {exc}",
              file=sys.stderr)
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def list_open_issues(repo: str, *, since: str | None = None) -> list[int] | None:
    """Every open issue number in `repo`, PRs excluded -- the candidate set
    for Lane 1's repo-wide newest-marker sweep (harmonic-forge#570 AC1/AC8).
    No marker search can pre-filter this the way `discover_queue` does:
    Lane 1 needs the newest comment on EVERY open issue, not just ones
    already carrying a specific marker, because the ball is with Lane 1
    whenever the newest classified comment is simply not its own.

    `since` (an ISO-8601 timestamp) narrows to issues updated at or after
    it -- the watermark `main()` threads through on every cycle after the
    first, so a steady-state Lane 1 sweep is bounded by recent activity
    rather than re-scanning every open issue's full comment history every
    poll (preclose finding: 155 open issues * one comments call each, every
    5 minutes, against a 5,000/hour shared quota).

    `-X GET` is required (preclose finding): `gh api` switches a request
    with `-f` parameters to POST unless told otherwise, and a POST to this
    endpoint is issue *creation*, which 422s with no `title` and is
    swallowed into an empty result -- reading as "no work," silently.

    Returns `None` -- distinct from `[]` -- when the fetch itself failed
    (network/auth/rate-limit), never a bare empty list (harmonic-forge#579
    AC1). A caller that can't tell "genuinely zero open issues" from "the
    call raised" cannot decide whether it's safe to advance a `since`
    watermark describing what this call covered; conflating the two is
    what let a transient failure silently narrow the next cycle's window
    and drop any issue updated during the lost window off Lane 1's belt
    for good.
    """
    args = ["api", "-X", "GET", f"repos/{repo}/issues", "--paginate",
            "-f", "state=open", "-f", "per_page=100"]
    if since:
        args += ["-f", f"since={since}"]
    args += ["--jq", ".[] | select(.pull_request == null) | .number"]
    try:
        raw = gh_as(_ACCOUNT, args, counter=_COUNTER)
    except Exception as exc:  # noqa: BLE001 — network/auth, reported not swallowed
        print(f"[watch_lane_posts] list_open_issues failed: {exc}", file=sys.stderr)
        return None
    try:
        return [int(line) for line in raw.splitlines() if line.strip()]
    except ValueError:
        return None


def _issue_is_open(repo: str, issue: int) -> bool:
    """Live open/closed check for one issue (harmonic-forge#579 AC4) --
    used only for `extra_issues` candidates that `list_open_issues`'s own
    `state=open` filter didn't already vouch for. On fetch failure, treat
    the issue as still open (fail toward keeping it queued, not toward
    silently dropping it -- the same fail-safe direction as the rest of
    this module's error handling)."""
    try:
        raw = gh_as(_ACCOUNT,
                    ["api", "-X", "GET", f"repos/{repo}/issues/{issue}", "--jq", ".state"],
                    counter=_COUNTER)
    except Exception as exc:  # noqa: BLE001 — network/auth, reported not swallowed
        print(f"[watch_lane_posts] _issue_is_open failed for #{issue}: {exc}", file=sys.stderr)
        return True
    return raw.strip() != "closed"


def discover_l1_sweep(
    repo: str, *, since: str | None = None,
    extra_issues: Mapping[int, tuple[str, str]] | Iterable[int] = (),
) -> tuple[dict[int, tuple[str, str]], bool]:
    """`({issue: (lane, detail)}, fetch_ok)` for every open issue whose newest
    classified comment is NOT Lane 1's own -- Lane 1's repo-wide newest-marker
    sweep, the mechanic named in prose under "Role: Lane 1" and given a
    runnable form here (harmonic-forge#570). An issue with no classified
    comment at all carries no ball to pick up and is excluded, not reported
    as queued.

    `since` bounds the *discovery* of NEW candidates to recently-updated
    issues -- see `list_open_issues`. Pass `None` (the default, and what the
    first cycle of any run must use) for a full scan. `extra_issues` must
    carry every issue the caller already believes is queued: an issue that
    stops receiving updates does not stop being queued, and dropping it
    from the candidate set the moment `since` excludes it would silently
    misreport it as resolved (`left-queue-for-l1`) rather than leave it
    queued, which is the opposite of what actually happened -- UNLESS the
    issue has actually been closed in the meantime, in which case it must
    drop out (harmonic-forge#579 AC4): each `extra_issues` candidate not
    already vouched for by `list_open_issues`'s own `state=open` filter is
    re-checked live via `_issue_is_open` before being kept.

    Pass a `{issue: (lane, detail)}` mapping (the caller's previously-
    queued classification) rather than a bare iterable of issue numbers
    when one is available -- it is the fallback used below when this
    cycle's own comment fetch for that issue fails, so a transient outage
    does not masquerade as "resolved" (harmonic-forge#579 preclose
    finding: the open-state re-check alone was not enough, because
    `_fetch_all_comments` failing separately for the same issue dropped it
    right back out on the very next step). A bare `Iterable[int]` still
    works (no fallback value on a failed comment fetch) for a caller with
    nothing to fall back to.

    `fetch_ok` is `False` when the underlying `list_open_issues` call itself
    failed (harmonic-forge#579 AC1) -- distinct from a fetch that succeeded
    and simply found nothing new. The caller must not advance a `since`
    watermark on a `False` result: doing so silently narrows the next
    cycle's window past whatever activity happened during the failed one."""
    extra_previous: dict[int, tuple[str, str]] = (
        dict(extra_issues) if isinstance(extra_issues, Mapping) else {}
    )
    extra_numbers = set(extra_previous) if extra_previous else set(extra_issues)
    fresh = list_open_issues(repo, since=since)
    fetch_ok = fresh is not None
    fresh_set = set(fresh or ())
    open_extra = {issue for issue in extra_numbers
                  if issue in fresh_set or _issue_is_open(repo, issue)}
    candidates = fresh_set | open_extra
    queued: dict[int, tuple[str, str]] = {}
    for issue in candidates:
        comments = _fetch_all_comments(repo, issue)
        if comments is None:
            # This cycle's comment fetch for `issue` failed -- fall back to
            # its previously-known classification rather than silently
            # excluding it, which would read identically to the issue
            # actually having been resolved (harmonic-forge#579 preclose
            # finding). No fallback value means no entry, matching this
            # function's behavior before that finding.
            previous = extra_previous.get(issue)
            if previous is not None:
                queued[issue] = previous
            continue
        last: tuple[str, str] | None = None
        for comment in comments:
            classified = _classify(comment.get("body", ""))
            if classified is not None:
                last = classified
        if last is not None and last[0] != "l1":
            queued[issue] = last
    return queued, fetch_ok


def discover_queue(repo: str, lane: str) -> dict[int, str]:
    """`{issue: kind}` for every open issue currently queued to `lane` --
    an l1-post marker whose kind is one of `QUEUE_KINDS[lane]` is the
    LATEST classified comment on that issue. Self-clearing: once anything
    is posted after that marker, the issue drops out on its own, so there
    is no separate "done" bookkeeping anywhere.

    A `## L2 Finding` comment is deliberately skipped when updating
    `last_kind` (harmonic-forge#580 AC1): it is visible to `_classify` (so
    a human or a different consumer of raw comments can see it), but it
    must not itself change ANY lane's queue membership. Before this fix, a
    finding posted after a `ready-for-l3` marker made `last_kind` become
    `('l2', ...)`, which failed the `last_kind[0] == 'l1'` check below and
    silently dropped a genuinely queued issue out of Lane 3's belt -- the
    exact live reproduction the issue's own AC1 names."""
    kinds = QUEUE_KINDS[lane]
    candidates: set[int] = set()
    for kind in kinds:
        candidates |= _search_candidates(repo, f"l1-post v1; kind={kind}")

    queued: dict[int, str] = {}
    for issue in candidates:
        last_kind: tuple[str, str] | None = None
        for comment in _fetch_all_comments(repo, issue) or ():
            # `or ()` -- harmonic-forge#579 preclose finding:
            # `_fetch_all_comments` returns `None` on a failed fetch (not
            # `[]`, which now means "genuinely zero comments"). This
            # function has no previous-classification fallback to offer
            # (unlike `discover_l1_sweep`), so a failed fetch here simply
            # yields no classified comments this cycle, exactly as an
            # empty result always has -- not a regression, just no longer
            # a `TypeError` from iterating `None`.
            classified = _classify(comment.get("body", ""))
            if classified is not None and not _is_l2_finding(*classified):
                last_kind = classified
        if last_kind and last_kind[0] == "l1" and last_kind[1] in kinds:
            queued[issue] = last_kind[1]
    return queued


def branch_ahead_without_completion(worktree: str, repo: str, issue: int) -> str | None:
    """harmonic-forge#583 AC4. A report string when `worktree`'s current
    branch carries commits ahead of `origin/main` that no completion post
    has announced on `(repo, issue)` -- the exact failure this issue names
    three times in one day (hrse#586, hrse#1675, hrse#1676): a branch
    finishes and the issue thread never says so, and only a human noticing
    reconciles the two. Returns `None` when there's nothing ahead to report,
    or when the LATEST `l2`-classified comment on the issue already is a
    completion (in sync) -- silence there is the correct, steady state, not
    a gap (contrast with this module's "empty reads as no new work"
    principle: this function's `None` is a real, checked negative, not an
    unattempted check).

    `origin/main` specifically, not a bare `main` -- a worktree's local
    `main` ref can itself be stale; comparing against the remote-tracking
    ref is what "ahead of the branch a merge would land on" actually means.
    Silently reports nothing (rather than raising) when `origin/main` can't
    be resolved at all -- an unfetched or non-standard remote is a worktree
    configuration question this function has no business surfacing as a
    lane-completion finding."""
    base = _run_git(worktree, "merge-base", "HEAD", "origin/main")
    if not base:
        return None
    count_text = _run_git(worktree, "rev-list", "--count", f"{base}..HEAD")
    if not count_text or not count_text.isdigit():
        return None
    count = int(count_text)
    if count == 0:
        return None
    last_l2: tuple[str, str] | None = None
    for comment in _fetch_all_comments(repo, issue) or ():
        classified = _classify(comment.get("body", ""))
        if classified is not None and classified[0] == "l2":
            last_l2 = classified
    if last_l2 is not None and _is_l2_completion(*last_l2):
        return None
    branch = _worktree_branch(worktree) or "?"
    plural = "" if count == 1 else "s"
    return (f"{repo}#{issue} branch {branch} is {count} commit{plural} ahead of "
            f"origin/main with no completion posted")


def l1_sweep_cycle(
    repo: str, l1_since: str | None, last_queue: dict[int, str], now: str,
) -> tuple[dict[int, tuple[str, str]], str | None]:
    """One Lane 1 sweep poll cycle's core logic -- factored out of `main()`
    (harmonic-forge#579 preclose finding) so AC1's watermark-gating
    behavior is directly unit-testable rather than only reachable through
    `main()`'s infinite polling loop, where no test exercised it (a
    reverted `if fetch_ok:` gate left the full suite green).

    Returns `(l1_queue, new_l1_since)`. `new_l1_since` is `now` only when
    `discover_l1_sweep`'s own fetch succeeded (AC1); otherwise it is
    `l1_since` unchanged, so the next cycle re-covers whatever window this
    one failed to see.

    `last_queue` holds `{issue: "lane:detail"}` (`main()`'s on-disk-free
    in-memory queue shape) -- split back into `{issue: (lane, detail)}`
    before passing to `discover_l1_sweep`, which uses it as its per-issue
    comment-fetch-failure fallback (harmonic-forge#579 preclose finding)."""
    previous = {issue: tuple(marker.split(":", 1)) for issue, marker in last_queue.items()}
    l1_queue, fetch_ok = discover_l1_sweep(repo, since=l1_since, extra_issues=previous)
    return l1_queue, (now if fetch_ok else l1_since)


def main() -> int:
    global _ACCOUNT
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
    parser.add_argument("--queue-for", choices=sorted({"l1", *QUEUE_KINDS}), default=None,
                        help="repo-wide: find ANY open issue currently queued to this lane "
                             "(paired with --repo), no worktree or issue number needed. "
                             "'l1' is Lane 1's newest-marker sweep (discover_l1_sweep) -- "
                             "every open issue whose newest comment isn't Lane 1's own -- "
                             "and is not a `QUEUE_KINDS` lookup like l2/l3.")
    parser.add_argument("--watch", action="append", default=[],
                        choices=["l1", "l2", "l3"],
                        help="lane whose posts to surface on watched issues -- l1, l2, "
                             "and/or l3 (repeatable). Not required when only --queue-for "
                             "is used -- queue entry/exit is its own event.")
    parser.add_argument("--interval", type=int, default=30, help="poll interval, seconds")
    parser.add_argument("--account", default=_ACCOUNT,
                        help="gh-as account slot every call is scoped to "
                             f"(default: {_ACCOUNT}). Its identity is asserted "
                             "before polling: a slot authenticating as someone "
                             "else refuses loudly rather than returning empty, "
                             "because empty reads as 'no new work'.")
    args = parser.parse_args()

    _ACCOUNT = args.account
    try:
        assert_identity(_ACCOUNT)
    except IdentityMismatch as exc:
        parser.error(str(exc))

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
    report_resolution(args.worktrees)

    last_discovered: set[tuple[str, int]] = set()
    last_queue: dict[int, str] = {}
    #: harmonic-forge#583 AC4. Keyed by worktree path (not `(repo, issue)`
    #: -- a worktree checks out a new branch/issue over its lifetime, and
    #: the report must clear the moment it does, not linger keyed to an
    #: issue nobody is on anymore). `None` is a real, checked "nothing to
    #: report" value, not "not yet checked" -- see `report != last_ahead.get`
    #: below, which only reprints on an actual state CHANGE.
    last_ahead: dict[str, str | None] = {}
    #: harmonic-forge#570 preclose finding: an unbounded full-repo comment
    #: scan every cycle (155 open issues on vitalharmony/hrse today) burns
    #: quota fast enough to exhaust it, and quota exhaustion is swallowed
    #: into an empty result -- which reads as "no work," the exact failure
    #: this protocol exists to refuse. `l1_since` narrows *new*-candidate
    #: discovery to issues updated since the last cycle; `last_queue`'s keys
    #: are always re-checked regardless (see `discover_l1_sweep`'s
    #: `extra_issues`), so an already-queued issue is never dropped just
    #: because it went quiet.
    l1_since: str | None = None
    #: A queue-for mode reports its queued count once at the first
    #: evaluation, even if it is zero -- silence and "confirmed watching
    #: nothing" must not look the same (harmonic-forge#570 preclose finding:
    #: AC6's guarantee was implemented for --worktrees only, and both
    #: prescribed Lane 1 and Lane 3 commands pass no --worktrees).
    first_queue_report = True
    while True:
        time.sleep(args.interval)
        now = _now()

        resolutions = [(path, *resolve_worktree(path)) for path in args.worktrees]
        discovered = {pair for _, pair, _ in resolutions if pair is not None}
        if discovered != last_discovered:
            print(f"[watch_lane_posts] now watching {sorted(discovered | static_pairs)}",
                  file=sys.stderr)
            report_resolution(args.worktrees)
            last_discovered = discovered

        if args.queue_for == "l1":
            l1_queue, l1_since = l1_sweep_cycle(args.repo, l1_since, last_queue, now)
            queue = {issue: f"{lane}:{detail}" for issue, (lane, detail) in l1_queue.items()}
            if first_queue_report:
                print(f"[watch_lane_posts] queue-for-l1: {len(queue)} issue(s) queued now",
                      file=sys.stderr)
                first_queue_report = False
            for issue, marker in queue.items():
                if last_queue.get(issue) != marker:
                    lane, detail = l1_queue[issue]
                    print(f"{args.repo}#{issue} needs-l1 last={lane} — {detail}")
                    sys.stdout.flush()
            for issue in set(last_queue) - set(queue):
                print(f"{args.repo}#{issue} left-queue-for-l1")
                sys.stdout.flush()
            last_queue = queue
        elif args.queue_for:
            queue = discover_queue(args.repo, args.queue_for)
            if first_queue_report:
                print(f"[watch_lane_posts] queue-for-{args.queue_for}: "
                      f"{len(queue)} issue(s) queued now", file=sys.stderr)
                first_queue_report = False
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

        #: harmonic-forge#583 AC4 -- only meaningful for a lane watching its
        #: own worktrees for `l2` completion signal; `--queue-for`-only runs
        #: (Lane 3's belt, e.g.) have no worktree of their own to check.
        if "l2" in watch:
            for path, pair, _reason in resolutions:
                if pair is None:
                    last_ahead.pop(path, None)
                    continue
                repo, issue = pair
                report = branch_ahead_without_completion(path, repo, issue)
                if report != last_ahead.get(path):
                    if report:
                        print(report)
                        sys.stdout.flush()
                    last_ahead[path] = report


if __name__ == "__main__":
    raise SystemExit(main())
