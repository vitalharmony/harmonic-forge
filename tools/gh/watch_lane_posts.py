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

**`--queue-for <lane>` no longer searches (harmonic-forge#686).** It used to
answer "which issue is currently queued to me, repo-wide, without anyone
naming a number" via an account-wide `gh api search/issues` call, once per
repo per `QUEUE_KINDS[lane]` kind, every poll cycle. That is exactly the
independent, issue-number-free discovery the operator's own ruling forbids
(harmonic-forge#640, restated 2026-09-18: *"You NEVER scan the repo EVER
looking for work"*) -- so it is gone, for every lane, not narrowed. What
`--queue-for` does now: it takes the candidate set the belt already holds
(worktree-resolved `(repo, issue)` pairs from `--all-worktrees`, any
explicit `--repo`/`--issues`, and any `(repo, issue)` `read_queue_candidates`
finds recently recorded -- see below) and, for each, re-checks the full
comment history to decide whether an `l1-post` marker of an eligible
`QUEUE_KINDS[lane]` kind is still the LATEST classified comment -- once
anything is posted after it, the issue drops out of the queue on its own,
with no separate "I'm done" bookkeeping anywhere. That per-issue re-check
is unchanged from before; only the part that used to invent the candidate
set is gone.

**Every lane's no-worktree inbound is covered by a fourth candidate source,
not lost -- Lane 3 included (harmonic-forge#691, rescoped).** A Lane 2
handoff, a Lane 1 Plan-First `plan`, and a Lane 3 `ready-for-l3`/`ae`/
`sweep`/`ae-and-sweep` are each posted on an issue that, by construction,
may have no worktree yet -- which is the exact gap harmonic-forge#596 and
#618 already fixed once, and which removing the account-wide search
without a replacement would have reopened. The replacement is not a scan:
`l1_post.py` (HRSE2), `l2_post.py` (this repo), and
`post_lane_discussion.py` (HRSE2, for the `plan` Lane 2 writes by hand and
Lane 3's `spec`/`gate-result`) are the only three places any `l1-post v1`
marker is ever written, so all three call the shared `belt_candidates.py`
module's `record_candidate(repo, issue, kind, posted_by)` on every
successful post, and `read_queue_candidates` (a thin wrapper over
`belt_candidates.read_candidates`, below) reads it -- no GitHub call, and a
candidate exists only because a session already handed it a number by
posting on it.

**Kind- and poster-filtered, not just repo and age (harmonic-forge#691
AC2').** The pre-rescope design recorded only `{repo, issue, posted_at}`,
so every issue ANY writer had ever touched in 14 days stayed a candidate
for EVERY lane -- measured at up to ~200 REST calls/tick against this
script's own ~17.6/tick baseline (`discover_queue` re-checks each
candidate's full comment history live). Recording `kind` and `posted_by`
lets `read_queue_candidates` pre-filter to what `discover_queue` would
accept anyway (`kind` in `QUEUE_KINDS[lane]`, `posted_by` in
`QUEUE_POSTERS[lane]`) before spending a single REST call, which is what
brings Lane 3's coverage in at the same near-zero cost as Lane 1's and
Lane 2's, rather than reopening the over-broad candidate set the kind-less
version risked. An earlier revision of this note called Lane 3's gap an
accepted design call; it was not structural, only that the pre-rescope
recorder didn't carry enough to tell Lane 3's inbound apart from Lane 2's
on the same file -- see "Role: Lane 3" in DESIGN.md.

Usage
-----
    # The Lane 1 case: every live worktree, repo-wide, plus --queue-for l1
    # re-checking any issue a worktree, --issues, or a recent l2_post.py
    # `plan` posting already names:
    python3 watch_lane_posts.py --all-worktrees --account-repos vitalharmony \\
        --queue-for l1 --watch l2 --watch l3 --interval 300 --deadline-seconds 1800

    # The Lane 2 case: both halves, same candidate-supplied contract.
    # --all-worktrees follows Lane 2 into its per-issue
    # /tmp/<repo>-<issue>-impl checkout; --queue-for l2 re-checks whichever
    # of those (plus any --issues, plus a recent l1_post.py `handoff`/
    # `rework` posting) carry an eligible marker:
    python3 watch_lane_posts.py --all-worktrees --account-repos vitalharmony \\
        --queue-for l2 --watch l1 --interval 300 --deadline-seconds 1800

    # The Lane 3 case: Lane 3 has no worktree of its own, so its candidate
    # set is whatever --repo/--issues names explicitly, plus (harmonic-forge#691
    # AC7') any recent l1_post.py ready-for-l3/ae/sweep/ae-and-sweep posting --
    # an empty set yields an empty queue, not a scan. The repo set for
    # --all-worktrees/--account-repos is still DERIVED, not listed (R-0122):
    python3 watch_lane_posts.py --queue-for l3 --account-repos vitalharmony \\
        --watch l1 --interval 300 --deadline-seconds 1800

    # No lane arms a repo-wide sweep. `--sweep-for l1` is retired
    # (harmonic-forge#640) and `--sweep-for l3` is retired
    # (harmonic-forge#659); both are refused at parse time.

**There is no manual-override / one-shot form any more (harmonic-forge#651
AC1, pitch-inspection override).** Every invocation -- Bash, Monitor,
`subprocess`, cron, a hand-typed debugging command -- reaches this same
`main()`, and `main()` has no branch that exits before its `while True:`
loop. So every invocation is belt mode, and every invocation is checked
against `CANONICAL_BELTS[os.environ["LANE"]]` and refused verbatim
(printing the exact command to copy) if it does not match. A static
`--repo OWNER/REPO --issues N --watch ...` debugging command that predates
this issue is no longer runnable this way -- there is no flag combination
exempt from the LANE/canonical-argv gate. Every lane has exactly one
table entry, its belt; Lane 3's former repo-wide sweep (`--sweep-for l3`)
is retired (harmonic-forge#659) and no longer in the table.

The exact tool calls a lane makes to arm (this Monitor command, plus the
`/loop` suspenders) are printed by `tools/lane/belt_plan.py`, and a
PreToolUse hook (`tools/hooks/enforce_belt_arming.py`) denies any other
arming call (harmonic-forge#659).

`--queue-for`, `--worktrees`/`--all-worktrees`, and `--repo`/`--issues` may
be combined; the watched set is their union, re-derived every cycle. The one
rejected combination is `--issues` with more than one repo -- an issue number
means nothing without exactly one repo to resolve it against. Exits only on
error or Ctrl-C; runs until stopped otherwise.

DO NOT paste a `--worktrees <static path>` command for a lane's belt. A
hardcoded worktree list goes stale the moment an ephemeral checkout appears
(harmonic-forge#590), and for Lane 2 the shared checkout it would name is the
one path Lane 2 is forbidden to work in.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from time import monotonic as _monotonic
from pathlib import Path
from typing import Any, Iterable, Mapping

sys.path.insert(0, str(Path(__file__).parent))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "onboard"))
import manifest as onboard_manifest  # noqa: E402

import belt_batch_view  # noqa: E402
import belt_candidates  # noqa: E402
from retired_artifacts import RETIRED_ARTIFACTS  # noqa: E402

from belt_mechanics import (  # noqa: E402
    CallCounter,
    IdentityMismatch,
    SeenSet,
    TickLog,
    Watermarks,
    assert_identity,
    gh_as,
    query_since,
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
#: Prefix letter -> repo, and the regex class built from it, both DERIVED from
#: `projects.toml` (harmonic-forge#605 preclose finding). Three hardcoded copies
#: of this map existed and onboarding openclaw exposed them: `O` was added to the
#: manifest and to `lane-shorthand.md`, and the belt still resolved nothing for a
#: branch like `l2/o12-fix` because this class read `[hHfFiI]`. A prefix table
#: that must be edited in four places to add a repo is the drift the manifest
#: exists to end -- so it is read, not restated.
_PREFIX_REPO = onboard_manifest.prefix_repos()

_BRANCH_ISSUE_RE = re.compile(
    r"(?:^|/)(?P<prefix>[" + "".join(sorted(
        {c for k in _PREFIX_REPO for c in (k.lower(), k.upper())})) +
    r"])?(?P<num>\d{2,6})(?=[-/]|$)")

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
    # harmonic-forge#618. Lane 1's belt is worktrees-first (#590), and a
    # Plan-First issue HAS NO WORKTREE until Lane 1 approves the plan -- the
    # branch is created in response to the approval. So the single most
    # time-sensitive thing Lane 1 owes was structurally invisible to it: four
    # plans (hrse#1383/#1662/#1663/#1771) sat unactioned until Lane 2 asked why
    # they were being ignored.
    #
    # This is the third instance of one property: A LANE'S INBOUND WORK HAS NO
    # WORKTREE, because the worktree is created in response to it. Lane 2's
    # handoff (#596), Lane 1's plan (here). Lane 3 never showed the symptom
    # because its inbound was queue-discovered from the start.
    #
    # `plan` ONLY, deliberately. Not `discussion` -- that was removed from
    # `l2`'s kinds on a measurement of 63 issues whose newest marker was a
    # discussion, none actionable, and adding it here would reintroduce that
    # noise on the lane with the least capacity to absorb it. The four stalled
    # plans were posted as `discussion`; harmonic-forge#618's guard in
    # `post_lane_discussion.py` is what makes `plan` the marker they carry.
    "l1": ("plan",),
}

#: WHO must have posted the marker for it to queue work TO a lane.
#:
#: harmonic-forge#618. This was hardcoded as `last_kind[0] == "l1"`, which is
#: correct for Lane 2 and Lane 3 -- Lane 1 hands work down -- and structurally
#: wrong for Lane 1, whose inbound is handed UP by Lane 2. Adding
#: `QUEUE_KINDS["l1"]` alone changed nothing: a `kind=plan` marker is
#: `posted-by=LANE2`, so the hardcoded check rejected it and the queue stayed
#: empty. Caught by running it rather than by reading it.
#:
#: Lane 1 accepts from l2 and l3 but never from itself -- "already acted" is
#: still expressed by Lane 1's own marker being newest.
QUEUE_POSTERS: dict[str, tuple[str, ...]] = {
    "l3": ("l1",),
    "l2": ("l1",),
    "l1": ("l2", "l3"),
}


#: Overlap `K`, in minutes. `query_since` reads from `min(watermark, now - K)`,
#: so the seam between cycles is re-read rather than assumed. 15 is a
#: deliberate over-cover of every prescribed interval (60s Lane 3, 90s Lane 2,
#: 300s Lane 1, 600s the sweep): the cost of re-reading is a larger response
#: the seen-set immediately dedups, and the cost of under-covering is a lost
#: comment. `SKILL.md` lists K as a parameter to tune from the first week's
#: tick log (harmonic-forge#599 AC3) -- it is named here so tuning it is an
#: edit to one constant with its rationale attached.
_OVERLAP_MINUTES = 15

#: Belt state, alongside `batch_auth`'s `~/.claude/state/batch-authorized.json`.
#: PERSISTENT, unlike the in-memory `since` this replaces: a restart previously
#: reset the window to `now`, silently skipping everything posted while the
#: belt was down -- which is exactly when a handoff is most likely to be missed.
_BELT_STATE = Path.home() / ".claude" / "state" / "belt"


def _wm_key(repo: str, issue: int) -> str:
    """Watermark key for one target. `/` is a path separator and `Watermarks`
    builds a filename from this, so it must not survive."""
    return f"{repo.replace('/', '__')}__{issue}"


def _parse_iso(stamp: str) -> dt.datetime:
    return dt.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=dt.timezone.utc)


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
    return _report_resolutions([(path, *resolve_worktree(path)) for path in worktrees])


def _report_resolutions(
    resolutions: list[tuple[str, tuple[str, int] | None, str]],
) -> list[tuple[str, tuple[str, int] | None, str]]:
    """`report_resolution`'s reporting half, over resolutions already computed.

    Split out by harmonic-forge#590 so the poll cycle can report the SAME list
    it acts on -- it filters closed issues out via `drop_closed_targets`, and a
    reporter that re-resolved from paths would print a target the belt is not
    actually watching."""
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


def _fetch_comments(repo: str, issue: int, since: str) -> list[dict] | None:
    try:
        raw = gh_as(
            _ACCOUNT,
            ["api", f"repos/{repo}/issues/{issue}/comments?since={since}"],
            counter=_COUNTER,
        )
    except Exception as exc:  # noqa: BLE001 — network/auth, reported not swallowed
        print(f"[watch_lane_posts] gh api failed for #{issue}: {exc}", file=sys.stderr)
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


#: Triple-backtick fenced code blocks, DOTALL so a multi-line fence is one
#: match. harmonic-forge#583 preclose finding / this file's own R-0334
#: (`rules/lane-shorthand.md`): "a marker quoted as evidence never counts as
#: a transition. Fenced blocks are stripped before any marker is read." --
#: this file never actually did that stripping; it only mattered for `kind=`
#: before `posted-by` became lane-determining, but a quoted footer NOW
#: silently reassigns which lane the whole comment is attributed to (a Lane
#: 1 comment pasting a Lane 2 completion footer as evidence would otherwise
#: classify as a real Lane 2 completion). No poster's own tooling
#: (`l1_post.py`, `l2_post.py`) ever emits its real marker inside a fence,
#: so stripping fenced content before the marker search cannot hide a
#: genuine one -- only a quoted one.
_FENCED_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)


def _strip_fenced_blocks(body: str) -> str:
    return _FENCED_BLOCK_RE.sub("", body)


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
    does not retroactively gain a marker.

    The marker search runs against the body with fenced code blocks
    stripped (`_strip_fenced_blocks`, R-0334) -- a marker quoted as
    evidence inside a fence must never be read as a real transition. The
    heading check below intentionally still uses the RAW body's first
    line: a heading is only ever meaningful as literally the first line of
    a real post, and no legitimate heading is fenced."""
    marker_match = _MARKER_RE.search(_strip_fenced_blocks(body))
    if marker_match:
        marker = marker_match.group(0)
        kind_match = _KIND_RE.search(marker)
        if kind_match:
            kind = kind_match.group(1)
            posted_by_match = _POSTED_BY_RE.search(marker)
            posted_by = posted_by_match.group(1) if posted_by_match else None
            return _POSTED_BY_LANE.get(posted_by, "l1"), kind
    headline = _mark_retired_tokens(body.strip().split("\n", 1)[0])
    if _L2_HEADING_RE.match(headline):
        return "l2", headline
    if _L3_HEADING_RE.match(headline):
        return "l3", headline
    return None


#: harmonic-forge#629. A Lane 3 `kind=gate-result` comment states its own
#: verdict as `**Verdict:** PASS|FAIL|BLOCKED|...` -- confirmed live against
#: the hrse#1792/#1771/#1798 gate-result comments this issue's own
#: regression case involved. `_classify` only reports the marker's `kind`
#: (`gate-result`), never this -- a second, narrower regex, because the
#: verdict is meaningful only for this one kind and nothing else in this
#: file needs it.
_VERDICT_RE = re.compile(r"\*\*Verdict:\*\*\s*(\w+)")

#: FAIL/BLOCKED are the two verdicts Check C watches for -- both mean the
#: gate did not close the issue, so Lane 1 owes the thread a response, same
#: as the two closest analogues (`R-0354`'s BLOCKED handling and a plain
#: gate FAIL). PASS, and any other verdict, never triggers this check.
_UNANSWERED_VERDICTS = frozenset({"FAIL", "BLOCKED"})


def _gate_verdict(body: str) -> str | None:
    """A Lane 3 gate-result comment's own stated verdict, or `None` if the
    body carries no `**Verdict:**` line (a comment classified `("l3",
    "gate-result")` by marker but written before this convention, or by a
    tool that doesn't follow it, still returns `None` here -- Check C's
    caller treats that as "nothing to watch," not an error)."""
    match = _VERDICT_RE.search(body)
    return match.group(1) if match else None


#: Retired lane tokens, and what replaced them. Read from the same registry
#: `gh_issue.py` checks issue bodies against (harmonic-forge#379) rather than
#: restated -- a second list is the drift this repo keeps paying for.
_RETIRED_TOKEN_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in RETIRED_ARTIFACTS
                      if re.fullmatch(r"L[123][A-Z]", k)) + r")\b"
) if any(re.fullmatch(r"L[123][A-Z]", k) for k in RETIRED_ARTIFACTS) else None


def _mark_retired_tokens(headline: str) -> str:
    """Annotate a retired lane token rather than reproducing it bare.

    harmonic-forge#609. The emitter stopped producing `L2P` at #583, but
    historical comments still carry it and this function renders a comment's
    first line straight into the lane's task display -- where it reads as
    current, because nothing said otherwise. An operator saw exactly that and
    had to correct Lane 1 by hand.

    Marking, not rewriting: the comment is an accurate record of what was
    posted in 2026-08 and must not be falsified. `## L2P` becomes
    `## L2P [retired -> L2S]`, which keeps the quote and stops the display
    teaching a token that no longer exists.
    """
    if _RETIRED_TOKEN_RE is None:
        return headline
    def _replace(match: re.Match) -> str:
        token = match.group(1)
        note = RETIRED_ARTIFACTS.get(token, "")
        successors = re.findall(r"`(L[123][A-Z])`", note)
        if not successors:
            return f"{token} [retired]"
        # " or ", not " -> ": L2P was replaced by a CHOICE between two tokens
        # (L2S for a plan, L2D for a completion), not by a sequence.
        return f"{token} [retired -> {' or '.join(successors)}]"
    return _RETIRED_TOKEN_RE.sub(_replace, headline)


# Queue-noise filters (operator ruling 2026-09-14, harmonic-forge#663). The belt listed every issue
# that ever got a handoff marker, including work that is not the lane's to
# pick up. Originally applied as search qualifiers (no extra calls); since
# harmonic-forge#686 removed the search these qualify, `queue_qualifiers`
# is `discover_queue`'s own label check below, not a second description of
# it -- the preclose finding was that the two had already drifted apart
# once the search was deleted: this function kept its old signature and
# docstring, unused, while `discover_queue` reimplemented the same two
# labels inline from the constants directly.
_LANE1_OWNED_LABEL = "tooling-exception"   # Lane 1 implements these
_NEVER_QUEUED_LABEL = "epic"               # never implemented directly
def queue_qualifiers(repo: str, lane: str) -> frozenset[str]:
    """Labels that keep work that isn't `lane`'s off its queue -- called
    from `discover_queue` per candidate issue (harmonic-forge#686; `repo`
    is accepted for a stable call shape and possible future per-repo
    exceptions, unused today).

    No milestone scoping (operator ruling 2026-09-14): measured live, the
    active resonance-chain workgroup sits in hrse's `3.0`/`Later` milestones,
    so excluding "future" milestones dropped the lane's actual queue."""
    labels = {_NEVER_QUEUED_LABEL}
    if lane in ("l2", "l3"):
        labels.add(_LANE1_OWNED_LABEL)
    return frozenset(labels)


def _bare_repo(repo: str, issue: int) -> str:
    """A tick-log ref in the ONE format `belt_report` accepts.

    `repo` is owner-qualified everywhere in this module (`vitalharmony/hrse`),
    but `belt_mechanics.REF_FORMAT` is `<repo>#<number>` and the reader's regex
    excludes `/`. Writing the owner-qualified form produces a "malformed entry"
    finding for every marker and drops it from the per-repo event counts —
    telemetry that reports zero events for a repo the belt is actively
    surfacing work in, which is the false-confidence failure this log exists to
    refuse (harmonic-forge#685 preclose finding).
    """
    return f"{repo.rsplit('/', 1)[-1]}#{issue}"


_LEADING_REF_RE = re.compile(r"^(?P<repo>[^\s#]+)#(?P<issue>\d+)\s")


def _record_line_refs(tick: "TickLog | None", lines: Iterable[str]) -> None:
    """Record every belt-emitted line as a matched-and-emitted tick ref
    (harmonic-forge#685 preclose finding 3). `queue_cycle` and
    `branch_ahead_lines` are the two emit sites `comment_watch_cycle`'s own
    `record_match`/`record_emit` pair never reached -- both print a
    `<repo>#<issue> ...` row with no tick call beside it, so every AE/sweep/
    queued-for-l3/branch-ahead event they surface was invisible to the log
    `belt_report.py` reads, even on a belt that otherwise ticked correctly.

    A `left-queue-for-<lane>` row is a RETRACTION, not a detection -- it
    names an issue that dropped out, carrying no marker to record, so it is
    deliberately skipped here rather than recorded as a match."""
    if tick is None:
        return
    for line in lines:
        if "left-queue-for-" in line:
            continue
        m = _LEADING_REF_RE.match(line)
        if m is None:
            continue
        ref = _bare_repo(m.group("repo"), int(m.group("issue")))
        tick.record_match(ref)
        tick.record_emit(ref)


def read_queue_candidates(
    repos: Iterable[str],
    lane: str,
    *,
    now: "dt.datetime | None" = None,
    base_dir: Path | None = None,
) -> set[tuple[str, int]]:
    """`(repo, issue)` pairs recorded recently whose newest entry is
    queue-eligible FOR `lane` (harmonic-forge#691 AC2') -- a thin wrapper
    over `belt_candidates.read_candidates` bound to THIS repo's live
    `QUEUE_KINDS`/`QUEUE_POSTERS`, so a candidate this function returns is
    always a candidate `discover_queue` would also consider eligible on
    that same tuple's kind/poster, not merely "some kind, some poster,
    recently."

    This is the replacement source for the no-worktree inbound coverage
    harmonic-forge#686 removed (a fresh Lane 2 `plan`, Lane 1 `handoff`/
    `rework`/`ready-for-l3`/`ae`/`sweep`, or Lane 3 spec/gate-result has no
    worktree until the other side acts on it, harmonic-forge#596/#618) --
    filtered by kind and poster, not just repo and age, which is what
    keeps the candidate set at single digits per tick instead of the
    kind-less design's measured ~200-calls/tick risk. No GitHub call on
    this side, still: `discover_queue`'s own per-issue re-check against the
    live comment thread is what actually decides membership."""
    return belt_candidates.read_candidates(
        repos, lane,
        queue_kinds=QUEUE_KINDS, queue_posters=QUEUE_POSTERS,
        now=now, base_dir=base_dir,
        #: Opt into the safe reader-side unlink (AC3') here, the one real
        #: call site that runs against the production directory -- a test
        #: never should (see `belt_candidates.read_candidates`'s own
        #: docstring on why the default is off).
        prune=True,
    )


#: `_search_candidates` was here. It ran `gh api -X GET search/issues`
#: once per repo per kind per cycle -- an issue-number-free, account-wide
#: scan -- and was the last caller of the search API in this file.
#: Removed by harmonic-forge#686: DESIGN.md's own ruling is that GitHub
#: enriches and does not discover, "regardless of which pull loop arms
#: it", and the operator restated it as "You NEVER scan the repo EVER
#: looking for work." `discover_queue` now takes its candidates from what
#: the belt already holds. Do not reintroduce a search here: retiring
#: `--sweep-for l3` (harmonic-forge#659) removed one flag and the same
#: call simply continued under `--queue-for`, which is how it survived.

def _issue_labels(repo: str, issue: int) -> set[str] | None:
    """Every label name on `issue`, or `None` if the fetch failed.

    A per-issue, bounded lookup — enrichment on a number `discover_queue`'s
    caller already holds, never a search. Restores the `queue_qualifiers`
    filter (`-label:epic`, `-label:tooling-exception` for l2/l3) that
    `_search_candidates` used to apply as a search qualifier before
    harmonic-forge#686 removed the search it was a qualifier ON — leaving the
    filter defined and correct but never called, and an epic or a
    Lane-1-owned Tooling Exception issue free to land on Lane 2/3's queue the
    moment its worktree exists."""
    try:
        raw = gh_as(
            _ACCOUNT,
            ["api", "-X", "GET", f"repos/{repo}/issues/{issue}",
             "--jq", ".labels[].name"],
            counter=_COUNTER,
        )
    except Exception as exc:  # noqa: BLE001 — reported, not swallowed
        print(f"[watch_lane_posts] label fetch failed for #{issue}: {exc}",
              file=sys.stderr)
        return None
    return {line for line in raw.splitlines() if line}


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


def discover_l3_unanswered_verdicts(
    repo: str, *, since: str | None = None,
    extra_issues: Mapping[int, str] | Iterable[int] = (),
) -> tuple[dict[int, str], bool]:
    """`({issue: verdict}, fetch_ok)` for every open issue whose LAST Lane-3
    `gate-result` comment stated `FAIL` or `BLOCKED`, AND has at least one
    later comment of any kind on the thread -- harmonic-forge#629, Check C.

    Structurally identical to `discover_l1_sweep` (this issue's own Design
    Alternatives section names it as the pattern to reuse) — same `since`/
    `extra_issues` split, same fetch-failure and closed-issue handling — but
    answers a different question: not "whose newest classified comment is
    not this lane's own" (that is `discover_queue`'s "still queued" check
    and `discover_l1_sweep`'s "needs Lane 1" check), but "did a FAIL/BLOCKED
    gate result ever get a reply, of ANY kind, classified or not."

    That "any kind" is the point (harmonic-forge#629's own regression case):
    hrse#1771's Lane 1 ruling after a FAIL gate-result was posted as
    `kind=discussion` — a real, substantive reply that neither Check A/B nor
    `discover_queue` (which only tracks `QUEUE_KINDS` markers) would ever
    surface, because a `discussion` marker carries no queue membership by
    design (harmonic-forge#570 measurement). Check C does not classify the
    reply at all — it only asks whether ANY comment landed after the
    FAIL/BLOCKED gate-result, timestamp-only, so a reply's `kind` (or
    absence of one) can never suppress it.

    An issue whose last Lane-3 gate-result was PASS, or that has no
    Lane-3 gate-result at all, or whose FAIL/BLOCKED gate-result has no
    later comment yet, carries no ball to watch and is excluded — same
    "excluded, not reported as queued" posture `discover_l1_sweep` states
    for an issue with no classified comment at all.
    """
    extra_numbers = set(extra_issues)
    fresh = list_open_issues(repo, since=since)
    fetch_ok = fresh is not None
    fresh_set = set(fresh or ())
    open_extra = {issue for issue in extra_numbers
                  if issue in fresh_set or _issue_is_open(repo, issue)}
    candidates = fresh_set | open_extra
    watching: dict[int, str] = {}
    for issue in candidates:
        comments = _fetch_all_comments(repo, issue)
        if comments is None:
            # Same fallback shape as `discover_l1_sweep`: a failed fetch
            # falls back to the previous cycle's classification rather than
            # silently reading as resolved.
            if isinstance(extra_issues, Mapping) and issue in extra_issues:
                watching[issue] = extra_issues[issue]
            continue
        last_gate_result: dict | None = None
        for comment in comments:
            if _classify(comment.get("body", "")) == ("l3", "gate-result"):
                last_gate_result = comment
        if last_gate_result is None:
            continue
        verdict = _gate_verdict(last_gate_result.get("body", ""))
        if verdict not in _UNANSWERED_VERDICTS:
            continue
        gate_time = last_gate_result.get("created_at", "")
        answered = any(
            comment.get("created_at", "") > gate_time
            for comment in comments if comment is not last_gate_result
        )
        if answered:
            watching[issue] = verdict
    return watching, fetch_ok


def l3_verdict_sweep_cycle(
    repo: str, l3_since: str | None, last_queue: dict[int, str], now: str,
) -> tuple[dict[int, str], str | None]:
    """One Check C poll cycle's core logic — mirrors `l1_sweep_cycle`
    exactly (same factoring rationale: directly unit-testable rather than
    only reachable through `main()`'s infinite loop). Returns
    `(watching, new_l3_since)`; `new_l3_since` advances to `now` only on a
    successful fetch, same watermark discipline as `l1_sweep_cycle`."""
    watching, fetch_ok = discover_l3_unanswered_verdicts(
        repo, since=l3_since, extra_issues=last_queue)
    return watching, (now if fetch_ok else l3_since)


def discover_queue(repo: str, lane: str,
                   candidates_for_repo: set[int]) -> tuple[dict[int, str], bool]:
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
    posters = QUEUE_POSTERS[lane]
    #: harmonic-forge#686. `candidates` was an account-wide `search/issues`
    #: scan -- one call per repo per kind per cycle, with no worktree and no
    #: issue number anywhere in it. That is precisely what DESIGN.md's own
    #: ruling forbids: *"GitHub enriches; it does not discover … never an
    #: independent, issue-number-free account-wide scan … regardless of which
    #: pull loop arms it"* (harmonic-forge#640, restated by the operator
    #: 2026-09-18: *"You NEVER scan the repo EVER looking for work."*)
    #:
    #: The ruling was written into the Lane 1 section and the code was
    #: lane-agnostic, so every lane's belt scanned -- `--sweep-for l3` was
    #: retired for this in harmonic-forge#659 and the same call simply
    #: continued under `--queue-for`.
    #:
    #: The caller now supplies the candidate set from what it already holds:
    #: worktree-resolved issue numbers, plus any `--repo/--issues` a human or
    #: another lane handed it. `_fetch_all_comments` below still enriches each
    #: one -- enrichment on a known number was never in question.
    candidates = set(candidates_for_repo)
    if not candidates:
        return {}, True

    excluded = queue_qualifiers(repo, lane)
    queued: dict[int, str] = {}
    for issue in candidates:
        labels = _issue_labels(repo, issue)
        if labels is not None and labels & excluded:
            # `queue_qualifiers`' filter, restored per-issue (harmonic-forge#686
            # preclose finding): an epic, or -- for l2/l3 -- a Lane-1-owned
            # Tooling Exception issue, is never queued to this lane even if a
            # worktree or an explicit --issues handed it in as a candidate.
            # Called through the shared function rather than reimplemented
            # from the constants here, so the two cannot drift apart again
            # (a second preclose finding: this block used to do exactly
            # that, leaving `queue_qualifiers` itself an untested orphan).
            continue
        last_kind: tuple[str, str] | None = None
        comments = _fetch_all_comments(repo, issue)
        if comments is None:
            # harmonic-forge#602 (found by an out-of-family review). `None` is
            # a FAILED fetch, distinct from `[]`. The note below was written
            # when nothing diffed this function's result against a previous
            # cycle, so yielding no classified comments was genuinely benign.
            # harmonic-forge#596 added exactly that diff -- `queue_cycle`
            # emits `left-queue-for-<lane>` for a prior key absent from the new
            # queue whenever the repo is considered successful -- which turned
            # this into a live retraction: a transient failure on ONE issue
            # tells the lane the ball moved on. Repo-level `fetch_ok` was the
            # fix for a repo-level failure and does not reach an issue-level
            # one, so this reports the repo as unreliable for this cycle.
            return {}, False
        for comment in comments:
            # harmonic-forge#579 introduced the `None` (failed) vs `[]`
            # (genuinely zero) distinction this loop now depends on; #602
            # retired the `or ()` that erased it. The note that used to sit
            # here said a failed fetch yielding no classified comments was
            # "not a regression" -- true only while nothing diffed this
            # function's result against a previous cycle, which #596 changed.
            classified = _classify(comment.get("body", ""))
            if classified is not None and not _is_l2_finding(*classified):
                last_kind = classified
        if last_kind and last_kind[0] in posters and last_kind[1] in kinds:
            queued[issue] = last_kind[1]
    return queued, True


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
    lane-completion finding.

    A FAILED comment fetch (`_fetch_all_comments` returning `None` --
    quota exhaustion, a transient 5xx) is also reported as `None`
    (harmonic-forge#583 preclose finding), never as "no completion posted":
    unlike `discover_queue`, where a failed fetch degrading to "no new
    classified comments this cycle" is a harmless no-op, HERE it would
    turn "I could not check" into a false positive assertion that a real
    completion doesn't exist. Silence for one cycle (the next successful
    poll re-checks from scratch) is the correct failure mode, not a
    confident wrong answer.

    A `finding` posted after the real completion must not un-classify it
    either (same #580 invariant `_is_l2_finding` protects in
    `discover_queue`, applied here too) -- a finding is a defect report,
    not a status transition, and must never overwrite the LATEST
    non-finding `l2` event when this function decides whether a completion
    already exists."""
    base = _run_git(worktree, "merge-base", "HEAD", "origin/main")
    if not base:
        return None
    count_text = _run_git(worktree, "rev-list", "--count", f"{base}..HEAD")
    if not count_text or not count_text.isdigit():
        return None
    count = int(count_text)
    if count == 0:
        return None
    comments = _fetch_all_comments(repo, issue)
    if comments is None:
        return None
    last_l2: tuple[str, str] | None = None
    for comment in comments:
        classified = _classify(comment.get("body", ""))
        if classified is not None and not _is_l2_finding(*classified) and classified[0] == "l2":
            last_l2 = classified
    if last_l2 is not None and _is_l2_completion(*last_l2):
        return None
    branch = _worktree_branch(worktree) or "?"
    plural = "" if count == 1 else "s"
    return (f"{repo}#{issue} branch {branch} is {count} commit{plural} ahead of "
            f"origin/main with no completion posted")


def branch_ahead_lines(
    resolutions: list[tuple[str, tuple[str, int] | None, str]],
    last_ahead: dict[str, str | None],
) -> list[str]:
    """One poll cycle's worth of AC4 output lines, factored out of `main()`
    (harmonic-forge#583 preclose finding) so this is directly unit-testable
    rather than only reachable through argv -- the exact factoring
    `l1_sweep_cycle` below already uses for the same reason.

    Takes EVERY resolved worktree unconditionally, with no dependence on
    `--watch` -- the preclose finding this exists to fix was a `"l2" in
    watch` gate that made the feature unreachable under every belt command
    `skills/belt-and-suspenders/SKILL.md` prescribes, because the lane that
    owns a worktree (Lane 2) is, by definition, watching OTHER lanes'
    posts (`--watch l1`), never its own. `last_ahead` is mutated in place
    (`main()`'s own state dict) so a worktree that stops resolving forgets
    its prior report rather than repeating it forever."""
    lines: list[str] = []
    for path, pair, _reason in resolutions:
        if pair is None:
            last_ahead.pop(path, None)
            continue
        repo, issue = pair
        report = branch_ahead_without_completion(path, repo, issue)
        if report != last_ahead.get(path):
            if report:
                lines.append(report)
            last_ahead[path] = report
    return lines


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


def manifest_repos(account: str) -> list[str]:
    """Every onboarded repo on `account`, from `projects.toml`.

    R-0122 requires the repo set to be DERIVED, not hand-maintained, and
    `projects.toml` is where this platform already derives it -- the manifest's
    own header says it exists because "duplication is the only source of drift,
    and drift here is silent: a repo missing from one copy still files issues,
    just onto the wrong board." harmonic-forge#596 first hardcoded four repos,
    then reached for `gh repo list`; the manifest is better than both. It costs
    no API call, it carries the checkout path, and it is the file a repo is
    onboarded through (R-0340), so a new repo is under the belt the moment it
    is onboarded rather than whenever someone remembers this list.

    `account` matters: ke'nekted is a separate account with separate
    credentials, and a vitalharmony-authed query against it returns EMPTY
    rather than erroring -- so filtering by account here is what keeps an
    unreachable repo from reading as a quiet one.
    """
    return sorted(p.repo for p in _manifest_projects(account) if p.repo)


def manifest_worktree_roots(account: str) -> list[str]:
    """Each onboarded repo's local checkout, from `projects.toml`.

    Uses the manifest's `checkout` rather than `<dir>/<repo name>`: HRSE2's
    directory is `HRSE2` while its manifest name is `hrse`, and harmonic-forge's
    checkout is `~/harmonic-forge` while its lane worktrees live in
    `~/Harmonic_Projects/`. A convention-based lookup gets both wrong -- it was
    tried in this issue and silently dropped hrse, the busiest repo of the four,
    reporting only `no checkout at ~/Harmonic_Projects/hrse`.

    A declared checkout that is not present is reported and skipped, not fatal:
    not everything is cloned, and the manifest deliberately keeps such a row
    rather than dropping it, "which would make a missing checkout
    indistinguishable from a repo nobody onboarded."
    """
    roots: list[str] = []
    for project in _manifest_projects(account):
        checkout = project.checkout
        if checkout is None:
            continue
        if (checkout / ".git").exists():
            roots.append(str(checkout))
        else:
            print(f"[watch_lane_posts]   {project.repo or project.name}: declared "
                  f"checkout {checkout} is not present -- skipped", file=sys.stderr)
    return roots


def _manifest_projects(account: str) -> list:
    try:
        projects = onboard_manifest.load()
    except Exception as exc:  # noqa: BLE001 — surfaced, never swallowed
        raise AccountReposUnavailable(
            f"could not read projects.toml: {exc}. Refusing to arm a belt on an "
            "unknown repo set -- an empty one reads as 'no work anywhere'.") from exc
    selected = [p for p in projects
                if p.onboarded and p.repo and (p.account or "vitalharmony") == account]
    if not selected:
        raise AccountReposUnavailable(
            f"projects.toml declares no onboarded repos for account {account!r} -- "
            "refusing to arm a belt that would watch nothing.")
    return selected


#: `SearchUnavailable` was here. It existed to distinguish "a `search/issues`
#: call failed" from "nothing is queued" -- meaningful only while
#: `_search_candidates` could raise it. harmonic-forge#686 removed the last
#: caller along with the search itself; `discover_queue` now fails closed on
#: an empty candidate SET (see its own docstring), which is a different
#: value with a different meaning and needs no exception to carry it.


class AccountReposUnavailable(Exception):
    """`gh repo list` failed or returned nothing usable (harmonic-forge#596).
    Fatal at arm time: a belt on an unknown repo set is worse than none."""


class RootNotARepo(Exception):
    """A path named to `--all-worktrees` is not a git repository
    (harmonic-forge#594). Raised, not warned: a named root is an assertion."""


def enumerate_worktrees(cwd: str | None = None) -> list[str]:
    """Every live worktree of the repo containing `cwd`, via `git worktree list`.

    harmonic-forge#590: Lane 1's belt is worktrees-first, and the set of
    worktrees is not static -- `/tmp/<repo>-<issue>-impl` checkouts appear and
    vanish per issue. A hardcoded `--worktrees` list therefore narrows the belt
    silently, which is the failure mode this protocol exists to avoid.

    Enumeration is re-run every poll cycle, not once at arm time (#590 preclose
    finding): a list read at arm time cannot go stale *between sessions*, which
    is what a hardcoded list gets wrong, but it goes stale *within* one the
    moment Lane 2 creates a worktree -- and the belt would then be blind to that
    issue for the session's whole life, with no line saying so.

    Returns [] and stays quiet on failure -- `enumerate_repo_roots` is the layer
    that reports a root contributing nothing, because only it knows how many
    other roots there were.
    """
    try:
        out = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=cwd, capture_output=True, text=True, check=True, timeout=15,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    return [line.split(" ", 1)[1].strip()
            for line in out.splitlines() if line.startswith("worktree ")]


def _git_common_dir(cwd: str | None = None) -> str | None:
    """The absolute `.git` common dir of the repo containing `cwd` -- the
    identity of a *repository*, shared by all of its worktrees. `None` if
    `cwd` is not inside a repo."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=cwd, capture_output=True, text=True, check=True, timeout=15,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    common = out.strip()
    if not common:
        return None
    # harmonic-forge#594: `~/harmonic-forge` is a symlink to
    # `~/Harmonic_Projects/harmonic-forge`. Two spellings of one repository must
    # collapse to one identity, or the duplicate-root report never fires for the
    # spelling pair most likely to be typed.
    return str(Path(common).resolve())


def enumerate_repo_roots(roots: list[str]) -> list[str]:
    """Union of every live worktree across each repo in `roots`, deduplicated
    by *repository*, reporting per root. `[]` means the repo containing CWD.

    Roots are NAMED, not inferred from the worktrees being watched
    (harmonic-forge#594). `git worktree list` only ever sees one repository, so
    a belt that must span hrse and harmonic-forge has to be told both -- and
    inferring the second from CWD made the command correct only when launched
    from the right directory, which is not a property a skill can arm.

    Three failures this makes loud. Each previously read as a healthy belt:

    - **Two roots, one repo.** Keyed by `--git-common-dir` with symlinks
      resolved, so `~/harmonic-forge` and `~/Harmonic_Projects/harmonic-forge`
      collapse to one identity and the duplicate is named.
    - **A named root that is not a repo** raises `RootNotARepo`. An explicitly
      named root contributing nothing is a typo, and arming a narrower belt
      than was asked for is the failure mode this whole protocol exists to
      refuse. (An unnamed CWD that is not a repo is only a warning -- nobody
      asserted it was one.)
    - **A root that contributes zero worktrees.** Invisible inside an aggregate
      count, so each root's own contribution is printed.
    """
    seen: dict[str, str] = {}          # common dir -> the root that claimed it
    discovered: set[str] = set()
    for root in (roots or [None]):
        label = root or "CWD"
        common = _git_common_dir(root)
        if common is None:
            if root is None:
                print("[watch_lane_posts]   root CWD: not a git repo -- contributes "
                      "nothing. Name the repo roots explicitly: "
                      "--all-worktrees <path> [<path> ...]", file=sys.stderr)
                continue
            raise RootNotARepo(
                f"--all-worktrees {root}: not a git repository. A named root that "
                "contributes nothing arms a narrower belt than you asked for, so "
                "this refuses rather than warns.")
        if common in seen:
            print(f"[watch_lane_posts]   root {label}: same repository as "
                  f"{seen[common]} -- contributes nothing new. Name a root in each "
                  "DIFFERENT repo the belt should span.", file=sys.stderr)
            continue
        seen[common] = label
        paths = enumerate_worktrees(root)
        print(f"[watch_lane_posts]   root {label}: {len(paths)} worktree(s)",
              file=sys.stderr)
        discovered.update(paths)
    return sorted(discovered)


#: Session cache of issues seen closed, keyed `(repo, issue)`. A closed issue's
#: abandoned `/tmp/<repo>-<issue>-impl` checkout lingers indefinitely -- nothing
#: prunes it -- and its branch reads as "ahead of origin/main" forever, because
#: main took the work as a squash merge. Without this the belt offers six such
#: ghosts on this machine right now (harmonic-forge#590 preclose finding).
#: Cached because re-asking every cycle for a state that essentially never goes
#: back is pure quota.
#:
#: harmonic-forge#640 preclose finding: this comment used to justify the cache
#: by saying "an issue reopened mid-session is picked up by the suspenders'
#: repo-wide sweep." That sweep is retired for Lane 1 (harmonic-forge#640) and
#: was never armed for Lane 2 (whose own worktrees also flow through this same
#: cache) -- so a reopened issue is now, honestly, invisible to the process
#: that cached it as closed until that process restarts. Accepted, not fixed
#: here: expiring or re-verifying this cache is a separate, larger change than
#: this issue's scope, and a genuinely reopened issue is a rare enough event
#: that a belt restart (which already happens routinely) recovers it.
_CLOSED_SEEN: set[tuple[str, int]] = set()


def drop_closed_targets(
    resolutions: list[tuple[str, tuple[str, int] | None, str]],
) -> list[tuple[str, tuple[str, int] | None, str]]:
    """Demote any resolution whose issue is closed to unresolved, naming it.

    A worktree is evidence that work *was* started, not that it is live. The
    belt is worktrees-first precisely to bound itself to work that exists;
    an abandoned checkout for a merged issue is not that, and offering it
    fails AC1 in the permissive direction -- the same direction, if not the
    same scale, as the repo-wide scan #590 removed.
    """
    kept: list[tuple[str, tuple[str, int] | None, str]] = []
    for path, pair, reason in resolutions:
        if pair is None:
            kept.append((path, pair, reason))
            continue
        repo, issue = pair
        if pair in _CLOSED_SEEN or not _issue_is_open(repo, issue):
            _CLOSED_SEEN.add(pair)
            kept.append((path, None,
                         f"{repo}#{issue} is closed -- abandoned worktree, not live work"))
            continue
        kept.append((path, pair, reason))
    return kept


def comment_watch_cycle(
    targets: list[tuple[str, int]],
    watch: set[str],
    now: str,
    watermarks: "Watermarks",
    seen: "SeenSet",
    primed_targets: set[str],
    tick: "TickLog | None" = None,
) -> tuple[list[str], bool]:
    """One comment-watch poll over `targets`, returning `(lines, fetch_failed)`.

    Extracted from `main()`'s loop by harmonic-forge#599's preclose finding:
    every behavior AC1/AC3/AC4/AC6 name lived inside `while True:` with no seam
    to drive one cycle, so six separate mutations of it -- including
    advance-on-failure, the exact regression AC6 names -- left the suite green.
    Logic that cannot be called cannot be tested.

    Three properties this holds:

    - **The watermark advances only on that target's own success.** A `None`
      fetch is a failure, not an empty result, and the next cycle re-reads.
    - **Priming is PER TARGET.** A scalar cleared once per cycle meant a target
      whose first fetch failed never got a priming pass, and then replayed its
      whole overlap window as new on the next cycle -- priming inverted into the
      thing it prevents.
    - **What priming suppressed is named, not counted.** The overlap window
      reaches 15 minutes backwards, so priming can now swallow a handoff posted
      moments before arming. A count cannot tell the operator that happened;
      the refs can, and the entry is permanent.

    `fetch_failed` (harmonic-forge#638 preclose finding) is `True` if ANY
    target's fetch failed this cycle -- "I do not know" must never read as
    "nothing found" for backoff purposes. A rate-limited belt that also backs
    off is finding out about real work later than a belt that just keeps
    retrying at the armed cadence.
    """
    lines: list[str] = []
    fetch_failed = False
    account = _ACCOUNT or "vitalharmony"
    for repo, issue in targets:
        target = f"{repo}#{issue}"
        mark = watermarks.get(account, _wm_key(repo, issue))
        comments = _fetch_comments(
            repo, issue,
            query_since(mark, _OVERLAP_MINUTES, _parse_iso(now)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"))
        if comments is None:
            print(f"[watch_lane_posts] {target}: comment fetch failed -- "
                  "watermark held, window will be re-read", file=sys.stderr)
            fetch_failed = True
            continue
        priming = target not in primed_targets
        suppressed: list[str] = []
        for comment in comments:
            classified = _classify(comment.get("body", ""))
            if classified is None:
                continue
            lane, detail = classified
            if lane not in watch:
                continue
            cid = str(comment.get("id", ""))
            if cid and cid in seen:
                continue
            if priming:
                if cid:
                    seen.add(cid, SeenSet.PRIMED)
                suppressed.append(f"{target} {lane} — {detail}")
                continue
            if cid:
                seen.add(cid, SeenSet.EMITTED)
            #: harmonic-forge#685. `created_at` is the COMMENT's own timestamp,
            #: not this tick's -- that distinction is the whole point of
            #: `record_emit`'s `posted_at` (harmonic-forge#519). A tick
            #: timestamp yields detection-to-action only; the interval between
            #: a marker being posted and a belt noticing it is the outage this
            #: telemetry exists to expose, and only the comment's own time can
            #: measure it.
            if tick is not None:
                #: `belt_mechanics.REF_FORMAT` is `<repo>#<number>` and
                #: `belt_report._REF` is `^[A-Za-z0-9._-]+#\d+$` -- no `/`. An
                #: owner-qualified `vitalharmony/hrse#1725` is rejected as a
                #: malformed entry and excluded from the per-repo counts, so a
                #: repo emitting on every tick would report `events=0` and trip
                #: the reader's own "silence is not evidence of quiet" alarm.
                #: `_bare_repo` is the only form the one reader accepts.
                tick.record_match(_bare_repo(repo, issue),
                                  posted_at=comment.get("created_at"))
                tick.record_emit(_bare_repo(repo, issue),
                                 posted_at=comment.get("created_at"))
            lines.append(f"{repo}#{issue} {lane} — {detail}")
        if priming:
            primed_targets.add(target)
            if suppressed:
                # Named, not counted (harmonic-forge#599 preclose finding): the
                # overlap window reaches backwards into live work, so this list
                # can contain a handoff posted minutes before arming. The
                # seen-set entry is permanent and deleting the watermark does
                # not undo it, so this print is the only record the operator
                # gets.
                print(f"[watch_lane_posts] {target}: primed (SUPPRESSED, not "
                      f"announced) {len(suppressed)} marker(s) already on the "
                      "thread at arm time:", file=sys.stderr)
                for line in suppressed:
                    print(f"[watch_lane_posts]     {line}", file=sys.stderr)
                print(f"[watch_lane_posts]   if one of those is live work, it "
                      f"will NOT be re-announced -- delete {seen.path} to "
                      "replay.", file=sys.stderr)
        watermarks.advance(account, _wm_key(repo, issue), _parse_iso(now))
    return lines, fetch_failed


#: harmonic-forge#638 AC3: a single global cap would converge every lane's
#: backoff to the same ceiling, erasing any urgency ordering distinct base
#: intervals encode. Scaling the cap off each lane's own base interval
#: preserves that for free, whatever the base is set to. (harmonic-forge#651:
#: every lane's canonical base interval is now 300s -- see CANONICAL_BELTS --
#: so today all three back off to the same 3000s cap; this function still
#: scales per-base rather than to a shared constant, in case a future lane
#: is armed at a different interval again. harmonic-forge#640 preclose
#: finding: an earlier version of this example named the now-retired Lane 1
#: sweep's 600s/6000s interval, which had no referent once that sweep was
#: retired.)
#: harmonic-forge#680. The container lifetime every canonical belt runs inside,
#: in seconds — the SINGLE source for it. `belt_plan.MONITOR_TIMEOUT_MS` is
#: derived from this (`* 1000`) rather than declared separately, and the same
#: value is emitted to the poller as `--deadline-seconds`, so the deadline the
#: loop holds and the timeout the Monitor is given cannot disagree.
#:
#: This direction is deliberate: `CANONICAL_BELTS` below must embed the flag
#: (the arming gate compares PARSED argument values, so a flag absent from the
#: table would make every canonical command fail the comparison), and the table
#: lives here. Declaring the number in `belt_plan` and importing it here would
#: be a circular import.
MONITOR_LIFETIME_S = 1800

_BACKOFF_FACTOR = 2.0
_BACKOFF_CAP_MULTIPLIER = 10.0


def next_poll_interval(base_interval: int, quiet_streak: int) -> int:
    """harmonic-forge#638 AC1/AC2/AC3: the next sleep, given how many
    CONSECUTIVE quiet cycles (no stdout lines) have just happened.

    AC1 -- exponential backoff, capped: `quiet_streak == 0` (the cycle that
    just found something, or the very first cycle) returns `base_interval`
    unchanged; each further quiet cycle roughly doubles it, up to
    `base_interval * _BACKOFF_CAP_MULTIPLIER`.

    AC2 -- this function cannot express "stop polling". Its return is
    always a positive, finite number of seconds -- there is no input, quiet
    for however long, that produces anything else. The belt's `while True:`
    loop calls `time.sleep()` on this return unconditionally; nothing here
    or in the caller ever branches to not sleep-and-continue.

    AC3 -- the cap is `base_interval * _BACKOFF_CAP_MULTIPLIER`, not a fixed
    number, so Lane 3's 60s base still backs off to a lower ceiling (600s)
    than the sweep's 600s base (6000s) -- the SAME relative urgency the
    armed intervals already encode is preserved at every backoff level, not
    just at the base.
    """
    if quiet_streak <= 0:
        return base_interval
    cap = base_interval * _BACKOFF_CAP_MULTIPLIER
    # AC2's own guarantee, made structural rather than asserted: `streak`
    # here is capped by the number of doublings that could possibly matter
    # (beyond this, `_BACKOFF_FACTOR ** streak` has already exceeded `cap`
    # for any realistic base/cap pair) -- a genuinely unbounded quiet run
    # (hours, days) must not compute `2.0 ** 10_000` and overflow, which is
    # exactly the "stop working" failure this AC exists to rule out.
    capped_exponent = min(quiet_streak, 32)
    return int(min(base_interval * (_BACKOFF_FACTOR ** capped_exponent), cap))


#: harmonic-forge#680. A deadline the loop will not schedule past, and a
#: reserve so the last poll happens INSIDE the window rather than racing the
#: kill.
_FINAL_POLL_RESERVE_S = 30


def sleep_before_next_poll(
    base_interval: int,
    quiet_streak: int,
    deadline: float | None,
    now: float,
) -> int | None:
    """Seconds to sleep, or `None` meaning "the window is over, stop".

    harmonic-forge#680. Two things the old `time.sleep(next_poll_interval(...))`
    could not do, and both caused a real miss:

    **1. The sleep never exceeds `base_interval` (NC1).** AC1 requires a post
    made at ANY point in the window to be emitted within one base interval.
    The first doubling already breaks that: at a 300s base, a post landing
    just after a poll that then sleeps 600s waits ~599s against a 300s bound.
    There is no cap value and no deadline logic that satisfies both — at a
    300s base with a 300s latency bound the backoff has zero headroom. So the
    backoff is not merely capped lower, it is not applied to the sleep at all.

    `next_poll_interval` is deliberately KEPT and still tested: its AC2
    property ("cannot express 'stop polling'") is worth preserving as an
    invariant, and a future lane armed at a longer base could use it with
    headroom. It is simply not what decides this sleep.

    **2. The loop cannot schedule past its own death.** The Monitor kills the
    process at a fixed lifetime; the old loop happily slept 1200s starting at
    t=600s in an 1800s container, so the third poll never ran and the belt was
    blind for the last ~20 minutes of every quiet window. With a deadline in
    hand that is structurally impossible: the sleep is clamped to the time
    remaining, and when there is not enough left for another poll the loop
    stops rather than sleeping into the kill.

    `deadline is None` (a belt started by hand, or an older invocation) keeps
    the previous unbounded behaviour rather than crashing — but the canonical
    arming path always supplies it.
    """
    interval = max(1, int(base_interval))
    if deadline is None:
        return interval
    remaining = deadline - now
    if remaining <= _FINAL_POLL_RESERVE_S:
        return None
    return int(min(interval, remaining - _FINAL_POLL_RESERVE_S))


def cycle_is_quiet(
    printed_anything: bool,
    queue: dict,
    mode: str | None,
    ok_repos: set[str],
    repos: list[str],
    comment_fetch_failed: bool,
) -> bool:
    """harmonic-forge#638 preclose findings 1/2: "did this cycle print a NEW
    line" is not the same signal as "there is currently no outstanding work" --
    dedup means a still-queued, unchanged item prints nothing, and a failed
    fetch also prints nothing. Both must block backoff, not be read as quiet.

    Finding 1 -- `queue_cycle` only emits a line on a queue-marker CHANGE, so
    real unpicked work sitting in `queue` unchanged across cycles would let
    the belt back off to 10x while it waits. `mode and queue` catches that:
    a non-empty queue in queue-for/sweep-for mode is never quiet, regardless
    of whether this cycle printed anything about it.

    Finding 1 (repo-fetch half) -- a repo that failed to report this cycle
    (`ok_repos` short of `repos`) must not be read as "nothing there either" --
    it's "unknown", and unknown must not read as quiet.

    Finding 2 -- `comment_watch_cycle`'s `fetch_failed` flag: "I do not know"
    must never read as "nothing found" for backoff purposes.
    """
    if printed_anything:
        return False
    if mode and queue:
        return False
    if mode and len(ok_repos) < len(repos):
        return False
    if comment_fetch_failed:
        return False
    return True


def queue_cycle(
    repos: list[str],
    lane: str,
    last_queue: dict[tuple[str, int], str],
    l1_since: dict[str, str | None],
    now: str,
    sweep: bool = False,
    batch_state_path: Path | None = None,
    candidate_pairs: set[tuple[str, int]] | None = None,
) -> tuple[dict[tuple[str, int], str], list[str], set[str]]:
    """One `--queue-for` poll across every repo: `(queue, lines, ok_repos)`.

    Extracted from `main()`'s loop by harmonic-forge#596's preclose finding.
    Every runtime behavior below was previously reachable only through the
    `while True:` loop, which no test calls -- so eight separate mutations of
    it (including reverting the repo-qualified key, and scanning only
    `repos[:1]`) left the suite fully green. Logic that cannot be called
    cannot be tested, and a guard suite that reads only doc text is not a
    substitute.

    Three properties this function exists to hold:

    - **Keys are `(repo, issue)`.** hrse#570 and harmonic-forge#570 both
      exist; a bare `int` key lets one evict the other.
    - **A repo whose fetch failed is not diffed.** `discover_queue` and
      `l1_sweep_cycle` both distinguish "nothing queued" from "I do not
      know", and only the first may produce `left-queue-for-*`. Retracting a
      queued issue because a search hit a rate limit tells the lane the ball
      moved on when it did not.
    - **`l1_since` advances per repo, and only on success** -- a watermark
      moved past a failed cycle silently narrows the next one.
    """
    #: harmonic-forge#686. The candidate set is now supplied by the caller
    #: from what it already holds, never discovered by a scan. `None` means
    #: "nothing in hand", which yields an empty queue rather than falling back
    #: to a search -- a silent fallback would reinstate the defect the moment
    #: a caller forgot the argument.
    candidate_pairs = candidate_pairs or set()
    queue: dict[tuple[str, int], str] = {}
    ok_repos: set[str] = set()
    for repo in repos:
        prior = {issue: last_queue[(r, issue)] for (r, issue) in last_queue if r == repo}
        checked: set[int] | None = None
        if sweep and lane == "l3":
            # harmonic-forge#629, Check C. `l1_since` is reused as a plain
            # per-repo watermark dict here, not Lane-1-specific -- only one
            # `--sweep-for` value runs per process, so there is never a
            # collision between the two sweep types sharing it. `prior`'s
            # values are this mode's own `"l3verdict:{verdict}"` markers
            # from the previous cycle (set below); strip the prefix back to
            # a bare verdict before handing them to `l3_verdict_sweep_cycle`
            # as its `extra_issues` fallback map.
            prior_verdicts = {issue: marker.removeprefix("l3verdict:") for issue, marker in prior.items()}
            l3_queue, since = l3_verdict_sweep_cycle(repo, l1_since.get(repo), prior_verdicts, now)
            fetch_ok = since == now
            l1_since[repo] = since
            found = {issue: f"l3verdict:{verdict}" for issue, verdict in l3_queue.items()}
            lane_label = "l3-sweep"
        elif sweep:
            l1_queue, since = l1_sweep_cycle(repo, l1_since.get(repo), prior, now)
            # `l1_sweep_cycle` encodes fetch_ok by whether it advanced the
            # watermark to `now` -- it returns the OLD `since` unchanged on a
            # failed fetch, precisely so the next cycle re-covers the window.
            fetch_ok = since == now
            l1_since[repo] = since
            found = {issue: f"{lane_}:{detail}"
                     for issue, (lane_, detail) in l1_queue.items()}
            lane_label = "l1-sweep"
        else:
            checked = {n for r, n in candidate_pairs if r == repo}
            raw, fetch_ok = discover_queue(repo, lane, checked)
            found = dict(raw)
            lane_label = lane
        if not fetch_ok:
            # Carry this repo's previous queue forward untouched, and keep it
            # OUT of `ok_repos` so the retraction pass below cannot see it.
            print(f"[watch_lane_posts]   {repo}: fetch failed -- carrying "
                  f"{len(prior)} previously-queued issue(s) forward, not retracting",
                  file=sys.stderr)
            for issue, marker in prior.items():
                queue[(repo, issue)] = marker
            continue
        ok_repos.add(repo)
        print(f"[watch_lane_posts]   {repo}: {len(found)} queued", file=sys.stderr)
        for issue, marker in found.items():
            queue[(repo, issue)] = marker
        if checked is not None:
            #: harmonic-forge#686 preclose finding 2. `checked` is the
            #: candidate set `discover_queue` actually inspected this cycle
            #: -- an issue in `prior` but NOT in `checked` was never looked
            #: at (its worktree is gone, or it was never named to `--issues`),
            #: not verified absent. Carrying it forward, same as the
            #: fetch-failed branch above, is what keeps a worktree
            #: disappearing from reading as "the ball moved on": only an
            #: issue this cycle actually checked and found unqueued may be
            #: retracted below.
            for issue, marker in prior.items():
                if issue not in checked and issue not in found:
                    queue[(repo, issue)] = marker

    lines: list[str] = []
    for (repo, issue), marker in queue.items():
        if last_queue.get((repo, issue)) == marker:
            continue
        if marker.startswith("l3verdict:"):
            verdict = marker.removeprefix("l3verdict:")
            lines.append(f"{repo}#{issue} needs-l1-response last-verdict={verdict}")
        elif sweep:
            last_lane, _, detail = marker.partition(":")
            lines.append(f"{repo}#{issue} needs-l1 last={last_lane} — {detail}")
        else:
            lines.append(f"{repo}#{issue} queued-for-{lane} kind={marker}")
    for repo, issue in set(last_queue) - set(queue):
        if repo in ok_repos:
            lines.append(f"{repo}#{issue} left-queue-for-{lane}")

    # harmonic-forge#600 AC1. A belt event that lands during a live batch used
    # to arrive with nothing said about the batch at all, so a session either
    # acted on it (breaking the batch's scope) or dropped it (losing the work),
    # and neither outcome left a trace. This says the third thing: it is
    # queued, it is not covered, and here is the remedy.
    #
    # On STDERR, beside the rows and never inside them -- AC5 keeps the stdout
    # contract's three row shapes exactly as they were, because a Monitor
    # parses them. And through `belt_batch_view`, which reads the state file
    # and imports nothing from `tools/hooks/` -- AC4.
    notice = belt_batch_view.deferral_notice(
        belt_batch_view.live_batch_keys(state_path=batch_state_path),
        len(lines),
    )
    if notice is not None:
        print(notice, file=sys.stderr)

    return queue, lines, ok_repos


#: harmonic-forge#651 AC1. Canonical belt/sweep argv per LANE, enforced
#: verbatim in `main()` so every invocation path (Bash, Monitor, `subprocess`,
#: cron) hits the same gate -- a control living only in `SKILL.md` prose is
#: exactly what the 2026-09-14 incident routed around. Interval 300
#: throughout (harmonic-forge#650 companion sets the same floor for the `gh`
#: shim/hook). One entry per lane. Lane 3's second entry, its repo-wide
#: sweep (`--sweep-for l3`), is retired (harmonic-forge#659): that sweep
#: exhausted the shared REST budget twice on 2026-09-14. `tools/lane/
#: belt_plan.py` builds the Monitor command from this table -- never retype it.
CANONICAL_BELTS: dict[str, list[dict[str, Any]]] = {
    "1": [
        {
            "argv": ["--all-worktrees", "--account-repos", "vitalharmony",
                      "--queue-for", "l1", "--watch", "l2", "--watch", "l3",
                      "--interval", "300",
                      "--deadline-seconds", str(MONITOR_LIFETIME_S)],
            "lock": "belt-lane1.lock",
        },
    ],
    "2": [
        {
            "argv": ["--all-worktrees", "--account-repos", "vitalharmony",
                      "--queue-for", "l2", "--watch", "l1", "--interval", "300",
                      "--deadline-seconds", str(MONITOR_LIFETIME_S)],
            "lock": "belt-lane2.lock",
        },
    ],
    "3": [
        {
            "argv": ["--queue-for", "l3", "--account-repos", "vitalharmony",
                      "--watch", "l1", "--interval", "300",
                      "--deadline-seconds", str(MONITOR_LIFETIME_S)],
            "lock": "belt-lane3.lock",
        },
    ],
}

#: Overridable in tests (`patch("watch_lane_posts.BELT_LOCK_DIR", tmp_path)`)
#: so a unit test never touches the operator's real cache directory.
BELT_LOCK_DIR = Path.home() / ".cache" / "harmonic-forge"


def _canonical_vars(parser: argparse.ArgumentParser, argv: list[str]) -> dict[str, Any]:
    ns = parser.parse_args(argv)
    d = vars(ns).copy()
    d["watch"] = sorted(d.get("watch") or [])
    return d


def _matching_canonical_entry(
    parser: argparse.ArgumentParser, args: argparse.Namespace, lane: str
) -> dict[str, Any] | None:
    actual = vars(args).copy()
    actual["watch"] = sorted(actual.get("watch") or [])
    for entry in CANONICAL_BELTS.get(lane, []):
        if actual == _canonical_vars(parser, entry["argv"]):
            return entry
    return None


def _enforce_canonical_belt(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> dict[str, Any]:
    """harmonic-forge#651 AC1. `main()` has no one-shot or test mode -- every
    invocation reaches the `while True:` loop below -- so every invocation is
    gated here, with no exempt flag combination (pitch-inspection named
    change). Refuses unless `LANE` names 1/2/3 and the parsed arguments match
    that lane's table entry exactly (compared as parsed values, `watch`
    sorted, so `--interval=300`/`--int 300`/reordering can't slip past --
    `allow_abbrev=False` on the parser closes the abbreviation form)."""
    lane = os.environ.get("LANE")
    if lane not in CANONICAL_BELTS:
        parser.error(
            "belt/sweep mode requires LANE=1|2|3 in the environment "
            "(harmonic-forge#651) -- launch via lane1/lane2/lane3, don't set "
            "LANE inline")
    entry = _matching_canonical_entry(parser, args, lane)
    if entry is None:
        canonical_cmds = "\n".join(
            "  " + " ".join(["watch_lane_posts.py", *e["argv"]])
            for e in CANONICAL_BELTS[lane]
        )
        parser.error(
            f"LANE={lane}'s arguments do not match its canonical command "
            f"(harmonic-forge#651) -- copy exactly:\n{canonical_cmds}")
    return entry


def _proc_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _nearest_session_pid(start_pid: int | None = None) -> int | None:
    """The nearest `claude`/`codex` ancestor of `start_pid` (default: this
    process) -- the "session" a belt lock holder belongs to, per
    pitch-inspection step 3. Best-effort via `/proc`; returns None if it
    can't be determined (non-Linux, sandboxed `/proc`, or no such ancestor),
    which callers treat as "unknown session" and fall back to the plain
    same-process duplicate check."""
    pid = start_pid or os.getpid()
    seen: set[int] = set()
    while pid and pid > 1 and pid not in seen:
        seen.add(pid)
        try:
            comm = Path(f"/proc/{pid}/comm").read_text().strip()
        except OSError:
            return None
        if comm in ("claude", "codex"):
            return pid
        try:
            status = Path(f"/proc/{pid}/status").read_text()
        except OSError:
            return None
        ppid = None
        for line in status.splitlines():
            if line.startswith("PPid:"):
                ppid = int(line.split(None, 1)[1])
                break
        if ppid is None:
            return None
        pid = ppid
    return None


def _parse_lock_holder(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _acquire_belt_lock(lock_name: str):
    """harmonic-forge#651 AC2. Exclusive, non-blocking `flock` on
    `BELT_LOCK_DIR/lock_name`, released automatically on process exit or
    crash (`flock` semantics -- no PID-file staleness window). On
    contention, resolved by pitch-inspection step 3's holder-liveness rule:

    - holder's session is dead: SIGTERM the holder pid, wait up to 5s,
      reclaim, and log the reclaim to stderr;
    - holder's session is alive and is the caller's own session (or session
      is unknown, the plain AC2 case): exit 4 silently on stdout -- a
      stderr line names the holder's pid/start time, but nothing is printed
      that would surface as a notification;
    - holder's session is alive and is a DIFFERENT session: print the
      refusal to STDOUT (so the arming session sees another session owns
      the belt), then exit 4.

    Returns the open file handle; keep it referenced for the process
    lifetime so the lock is held.
    """
    BELT_LOCK_DIR.mkdir(parents=True, exist_ok=True)
    path = BELT_LOCK_DIR / lock_name
    fh = open(path, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.seek(0)
        holder = _parse_lock_holder(fh.read().strip())
        my_session = _nearest_session_pid()
        holder_pid = holder.get("pid") if holder else None
        holder_start = holder.get("start") if holder else None
        holder_session = holder.get("session_pid") if holder else None

        if holder_session and not _proc_alive(holder_session):
            if holder_pid:
                try:
                    os.kill(holder_pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
            deadline = time.time() + 5
            reclaimed = False
            while time.time() < deadline:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    reclaimed = True
                    break
                except BlockingIOError:
                    time.sleep(0.2)
            if not reclaimed:
                print(f"[watch_lane_posts] belt lock {lock_name}: holder pid "
                      f"{holder_pid} would not release after SIGTERM -- "
                      "exiting", file=sys.stderr)
                sys.exit(4)
            print(f"[watch_lane_posts] belt lock {lock_name}: prior holder's "
                  f"session (pid {holder_session}) was dead -- reclaimed "
                  f"from pid {holder_pid}", file=sys.stderr)
        elif holder_session and my_session and holder_session != my_session:
            print(f"belt already running for lock {lock_name} (pid "
                  f"{holder_pid} since {holder_start}, session "
                  f"{holder_session}); this duplicate exits by design -- "
                  "another session owns this belt")
            sys.exit(4)
        else:
            print(f"belt already running for lock {lock_name} (pid "
                  f"{holder_pid} since {holder_start}); this duplicate exits "
                  "by design", file=sys.stderr)
            sys.exit(4)
    fh.seek(0)
    fh.truncate()
    fh.write(json.dumps({
        "pid": os.getpid(),
        "start": _now(),
        "session_pid": _nearest_session_pid(),
    }))
    fh.flush()
    return fh


def _check_git_staleness(
    parser: argparse.ArgumentParser, script_dir: Path | None = None
) -> None:
    """harmonic-forge#651 AC3. Local-git-only, no REST budget cost: fetch the
    remote-tracking ref (`git fetch --quiet origin main`), then refuse belt
    mode if HEAD is behind it. Fails OPEN (warns, continues) on any git
    error -- a broken git invocation must not become a second way to
    silently disable every lane's belt.

    `script_dir` defaults to this module's own directory; tests pass a
    throwaway repo instead of touching the real harmonic-forge checkout."""
    script_dir = script_dir or Path(__file__).resolve().parent
    try:
        root_proc = subprocess.run(
            ["git", "-C", str(script_dir), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=7)
        if root_proc.returncode != 0:
            raise RuntimeError(root_proc.stderr.strip())
        repo_root = root_proc.stdout.strip()
        subprocess.run(["git", "-C", repo_root, "fetch", "--quiet", "origin", "main"],
                       capture_output=True, text=True, timeout=15)
        head = subprocess.run(["git", "-C", repo_root, "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=7).stdout.strip()
        remote = subprocess.run(["git", "-C", repo_root, "rev-parse", "origin/main"],
                                capture_output=True, text=True, timeout=7).stdout.strip()
        count_proc = subprocess.run(
            ["git", "-C", repo_root, "rev-list", "--count", "HEAD..origin/main"],
            capture_output=True, text=True, timeout=7)
        if count_proc.returncode != 0:
            raise RuntimeError(count_proc.stderr.strip())
        behind = int(count_proc.stdout.strip())
    except Exception as exc:  # fail open, deliberately broad
        print(f"[watch_lane_posts] staleness check failed ({exc}) -- "
              "continuing (fail-open)", file=sys.stderr)
        return
    if behind > 0:
        parser.error(
            f"~/harmonic-forge is {behind} commit(s) behind origin/main "
            f"(HEAD={head[:12]}, origin/main={remote[:12]}) -- relaunch via "
            "lane1/lane2/lane3 to pick up the pull (harmonic-forge#651)")


def _build_parser() -> argparse.ArgumentParser:
    """Split out of `main()` (harmonic-forge#651) so `CANONICAL_BELTS`
    enforcement -- both at runtime (`_enforce_canonical_belt`) and in
    `test_belt_skill_matches_table.py`'s doc-sync check -- normalizes a
    canonical or extracted command through the EXACT SAME parser `main()`
    runs, never a second hand-built one that could quietly drift from it."""
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     allow_abbrev=False)
    parser.add_argument("--worktrees", nargs="+", default=[],
                        help="worktree path(s) -- (repo, issue) re-derived from each one's "
                             "CURRENT branch every poll cycle, so this follows a lane across "
                             "issues with zero reconfiguration")
    parser.add_argument("--all-worktrees", nargs="*", metavar="REPO_ROOT",
                        default=None,
                        help="enumerate every live worktree of each named repo via `git "
                             "worktree list` and watch all of them -- harmonic-forge#590. "
                             "A hardcoded --worktrees list goes stale the moment an "
                             "ephemeral /tmp/<repo>-<issue>-impl worktree is created or "
                             "removed, and a narrowed belt is silent, not loud. `git "
                             "worktree list` sees ONE repository, so name a path in each "
                             "repo the belt should span -- that is what lets one belt cover "
                             "hrse and harmonic-forge, and naming them makes the command "
                             "correct from any directory (harmonic-forge#594). With no "
                             "paths, enumerates the repo containing CWD.")
    parser.add_argument("--sweep-for", choices=("l1", "l3"), metavar="LANE",
                        help="RETIRED for every value -- both are refused at parse "
                             "time. `l3` (harmonic-forge#659, operator ruling: it "
                             "exhausted the shared REST budget twice on 2026-09-14) was "
                             "the unanswered-verdict watch (`discover_l3_unanswered_"
                             "verdicts`, harmonic-forge#629 Check C) -- every open "
                             "issue whose last Lane 3 gate-result was FAIL/BLOCKED and "
                             "has since received ANY reply, classified or not (a "
                             "`kind=discussion` ruling included -- that gap is exactly "
                             "what Check C exists to close). Unbounded by design -- "
                             "structurally what the belt cannot see. `l1` is a listed "
                             "choice but REFUSED at parse time (harmonic-forge#640, "
                             "operator ruling): Lane 1's newest-marker sweep "
                             "(`discover_l1_sweep`) is retired, and its bounded "
                             "replacement is `--queue-for l1` (harmonic-forge#618), "
                             "not this flag.")
    parser.add_argument("--account-repos", metavar="ACCOUNT",
                        help="derive the repo set from projects.toml, the onboarded-repo "
                             "manifest, for ACCOUNT -- R-0122 and this protocol's design "
                             "note both "
                             "require the set to be DERIVED, not hand-maintained, so a "
                             "new repo is covered with no edit and an archived one drops "
                             "out. Feeds --queue-for (as repos) and --all-worktrees (as "
                             "roots, via each repo's checkout under --checkout-dir). "
                             "Fails hard if the list cannot be fetched: an empty repo set "
                             "reads as 'no work anywhere'.")
    parser.add_argument("--repo", action="append", metavar="OWNER/REPO",
                        help="owner/repo for a manual --issues override, or the repo(s) "
                             "--queue-for scans. Repeatable: a lane carries work in hrse "
                             "AND harmonic-forge, and a belt that scans one of them is a "
                             "half-belt (harmonic-forge#596).")
    parser.add_argument("--issues", type=int, nargs="+", default=[],
                        help="issue numbers to poll, paired with --repo (static, not "
                             "re-derived) -- for watching an issue with no worktree")
    parser.add_argument("--queue-for", choices=sorted(QUEUE_KINDS), default=None,
                        help="repo-wide: find ANY open issue currently queued to this lane "
                             "(paired with --repo/--account-repos), no worktree or issue "
                             "number needed. BOUNDED for every lane, including l1 since "
                             "harmonic-forge#618: an issue queues only when its newest "
                             "classified comment is of a kind in QUEUE_KINDS[lane] AND was "
                             "posted by a lane in QUEUE_POSTERS[lane]. The unbounded "
                             "newest-marker sweep is a DIFFERENT flag, --sweep-for.")
    parser.add_argument("--watch", action="append", default=[],
                        choices=["l1", "l2", "l3"],
                        help="lane whose posts to surface on watched issues -- l1, l2, "
                             "and/or l3 (repeatable). Not required when only --queue-for "
                             "is used -- queue entry/exit is its own event.")
    parser.add_argument("--interval", type=int, default=30, help="poll interval, seconds")
    parser.add_argument("--deadline-seconds", type=int, default=None,
                        help="the container lifetime this belt runs inside "
                             "(harmonic-forge#680). The loop never schedules a "
                             "sleep that ends after it, and stops rather than "
                             "sleeping into the kill. Supplied by belt_plan.py "
                             "from MONITOR_TIMEOUT_MS, so the deadline and the "
                             "Monitor's own timeout are one number, not two "
                             "that must agree. Omitted means unbounded, the "
                             "pre-#680 behaviour.")
    parser.add_argument("--account", default=_ACCOUNT,
                        help="gh-as account slot every call is scoped to "
                             f"(default: {_ACCOUNT}). Its identity is asserted "
                             "before polling: a slot authenticating as someone "
                             "else refuses loudly rather than returning empty, "
                             "because empty reads as 'no new work'.")
    return parser


def main() -> int:
    global _ACCOUNT
    parser = _build_parser()
    args = parser.parse_args()

    _ACCOUNT = args.account
    try:
        assert_identity(_ACCOUNT)
    except IdentityMismatch as exc:
        parser.error(str(exc))

    if args.issues and not args.repo:
        parser.error("--issues requires --repo")
    if (args.queue_for or args.sweep_for) and not (args.repo or args.account_repos):
        parser.error("--queue-for/--sweep-for requires --repo or --account-repos")
    if args.queue_for and args.sweep_for:
        parser.error("--queue-for and --sweep-for are the belt and the suspenders "
                     "respectively; arming both in one process collapses two "
                     "deliberately independent mechanisms (harmonic-forge#590)")
    if args.sweep_for == "l1":
        parser.error("--sweep-for l1 is RETIRED (harmonic-forge#640, operator "
                      "ruling). Lane 1 discovery is worktree-bounded: --all-worktrees "
                      "for the belt, plus the bounded --queue-for l1 Plan-First catch. "
                      "GitHub enriches an issue a worktree already named; it is never "
                      "asked to name candidates. If you found this command in an old "
                      "transcript or SKILL.md copy, that copy is stale.")
    if args.sweep_for == "l3":
        parser.error("--sweep-for l3 is RETIRED (harmonic-forge#659, operator "
                      "ruling): the repo-wide sweep exhausted the account's shared "
                      "REST budget twice on 2026-09-14 (19:33 and 21:32 UTC). No lane "
                      "runs a repo-wide sweep. Lane 3's only belt is --queue-for l3; "
                      "run `python3 ~/harmonic-forge/tools/lane/belt_plan.py` for the "
                      "exact arming calls. If you found this command in an old "
                      "transcript or SKILL.md copy, that copy is stale.")
    _belt_lock_entry = _enforce_canonical_belt(parser, args)
    _check_git_staleness(parser)
    _belt_lock_handle = _acquire_belt_lock(_belt_lock_entry["lock"])  # noqa: F841
    # Union, not replacement: an explicitly named --worktrees path stays
    # watched. It no longer doubles as a repo-root seed (harmonic-forge#594) --
    # roots are named to --all-worktrees, so --worktrees has one job again.
    explicit_worktrees = list(args.worktrees)
    repo_roots = args.all_worktrees          # None = flag absent; [] = bare flag

    def current_worktrees() -> list[str]:
        if repo_roots is None:
            return explicit_worktrees
        # Re-read every cycle (harmonic-forge#590): `repo_roots` is stable, but
        # the worktrees inside each root are not.
        print("[watch_lane_posts] --all-worktrees enumerating:", file=sys.stderr)
        return sorted(set(explicit_worktrees) | set(enumerate_repo_roots(repo_roots)))

    if repo_roots is not None and args.account_repos:
        # --account-repos supplies the roots so --all-worktrees needs no paths:
        # the repo set is derived once, and each repo's local checkout is found
        # by convention under --checkout-dir (harmonic-forge#596).
        try:
            repo_roots = list(repo_roots) + manifest_worktree_roots(
                args.account_repos)
        except AccountReposUnavailable as exc:
            parser.error(str(exc))
    if repo_roots is not None:
        try:
            args.worktrees = current_worktrees()
        except RootNotARepo as exc:
            parser.error(str(exc))
        print(f"[watch_lane_posts] --all-worktrees enumerated "
              f"{len(args.worktrees)} live worktree(s)", file=sys.stderr)
    if not args.worktrees and not args.issues and not (args.queue_for or args.sweep_for):
        parser.error("give at least one of --worktrees, --repo/--issues, "
                     "--all-worktrees, or --queue-for")
    if not args.watch and not (args.queue_for or args.sweep_for):
        parser.error("--watch is required unless --queue-for is given")

    watch = set(args.watch)
    repos: list[str] = list(args.repo or [])
    if args.account_repos:
        try:
            derived = manifest_repos(args.account_repos)
        except AccountReposUnavailable as exc:
            parser.error(str(exc))
        print(f"[watch_lane_posts] --account-repos {args.account_repos}: "
              f"{len(derived)} non-archived repo(s)", file=sys.stderr)
        repos = sorted(set(repos) | set(derived))
    if len(repos) > 1 and args.issues:
        parser.error("--issues takes a single --repo: an issue number means nothing "
                     "without exactly one repo to resolve it against")
    static_pairs = {(repos[0], n) for n in args.issues} if repos else set()
    since = _now()
    print(f"[watch_lane_posts] worktrees={args.worktrees or None} "
          f"static={sorted(static_pairs) or None} queue_for={args.queue_for or None} "
          f"lanes={sorted(watch) or None} every {args.interval}s", file=sys.stderr)
    _report_resolutions(drop_closed_targets(
        [(path, *resolve_worktree(path)) for path in args.worktrees]))

    # Keyed by BELT IDENTITY, not just by target (harmonic-forge#599 preclose
    # finding). Lane 1 (`--watch l2 --watch l3`) and Lane 2 (`--watch l1`) are
    # prescribed to run simultaneously and both enumerate the same worktrees, so
    # a shared file meant Lane 2's successful cycle advanced past the window
    # Lane 1 was down for -- and Lane 1 never read it, in that session or any
    # later one. `Watermarks`' own docstring makes this argument one level up
    # about accounts vs repos; the process axis was the one left unkeyed.
    belt_id = "-".join(sorted(watch)) or "none"
    if args.queue_for:
        belt_id += f"+q{args.queue_for}"
    if args.sweep_for:
        belt_id += f"+s{args.sweep_for}"
    watermarks = Watermarks(_BELT_STATE / "watermarks" / belt_id)
    seen = SeenSet(_BELT_STATE / f"seen-{belt_id}.tsv")
    #: harmonic-forge#685. Keyed by `belt_id` for the same reason the seen-set
    #: is: two belts with different watch/queue arguments are different
    #: processes and their ticks must not interleave into one file.
    #: `DESIGN.md:529` requires this path be reported at arm time.
    tick_log_path = _BELT_STATE / f"ticks-{belt_id}.jsonl"
    lane_label = args.queue_for or (sorted(watch)[0] if watch else "none")
    print(f"[watch_lane_posts] tick log: {tick_log_path}", file=sys.stderr)
    #: Targets this belt has already primed. PER TARGET, not one scalar for the
    #: run: a target whose first fetch failed never got a priming pass, then
    #: replayed its whole overlap window as new -- priming inverted into the
    #: thing it exists to prevent.
    primed_targets: set[str] = set()
    # A target already in the seen-set was primed by an earlier session, so it
    # must not be primed again -- re-priming would suppress live work.
    if seen._state:
        print(f"[watch_lane_posts] resuming: {len(seen._state)} comment(s) "
              f"already recorded in {seen.path.name}", file=sys.stderr)
    else:
        print("[watch_lane_posts] first arm for this belt: each target's "
              "opening cycle PRIMES (records without announcing) so arming does "
              "not replay history. Suppressed markers are listed per target.",
              file=sys.stderr)

    last_discovered: set[tuple[str, int]] = set()
    last_queue: dict[tuple[str, int], str] = {}
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
    l1_since: dict[str, str | None] = {}
    #: A queue-for mode reports its queued count once at the first
    #: evaluation, even if it is zero -- silence and "confirmed watching
    #: nothing" must not look the same (harmonic-forge#570 preclose finding:
    #: AC6's guarantee was implemented for --worktrees only, and both
    #: prescribed Lane 1 and Lane 3 commands pass no --worktrees).
    first_queue_report = True
    #: harmonic-forge#638 AC1/AC5: consecutive cycles with zero stdout lines.
    #: Reset to 0 the moment any cycle finds something; the sleep at the
    #: bottom of the loop backs off with it via `next_poll_interval()`.
    quiet_streak = 0
    #: harmonic-forge#680 AC3: counted, not derived. The window's own poll
    #: count is printed when it ends.
    polls_this_window = 0
    #: Monotonic so a wall-clock adjustment mid-window cannot move the
    #: deadline. `None` keeps the pre-#680 unbounded loop.
    deadline = (_monotonic() + args.deadline_seconds
                if args.deadline_seconds else None)
    #: The interval actually used last cycle, so a CHANGE (not every cycle)
    #: gets a stderr line -- AC5's measured-saving record, not silent tuning.
    last_reported_interval = args.interval
    # Poll FIRST, sleep after (harmonic-forge#596). Sleeping first meant a belt
    # printed nothing until one whole interval had elapsed -- ten minutes of
    # silence for the suspenders' 600s sweep, which is documented as a one-shot
    # backstop you run and read. "A monitor that never printed a status line is
    # not proof it is watching anything" is this protocol's own rule; making the
    # operator wait an interval to find out is the same failure, deferred.
    while True:
        now = _now()
        #: harmonic-forge#685. One record per tick, written unconditionally
        #: near the bottom of this loop body (there is no `finally` -- an
        #: uncaught exception this cycle skips the write, same as it skips
        #: everything else below it; this loop has no exception handling to
        #: hook one onto) so a quiet tick is still recorded even when nothing
        #: is printed. `DESIGN.md:360`: "a quiet tick that writes nothing is
        #: indistinguishable from a dead monitor" -- so the write does not
        #: depend on `cycle_emitted`, while chat output still does.
        #: The class has existed and been tested since harmonic-forge#519 with
        #: no caller; this is the caller.
        tick = TickLog(path=tick_log_path, lane=lane_label, trigger="belt")
        #: `_COUNTER` is module-level and accumulates for the life of the
        #: process, so handing it to the tick directly would report a running
        #: total as this tick's cost — rising every cycle and never matching
        #: what the tick actually spent. Snapshot here, subtract at write.
        #: A zero left in the record would be worse than absent: `belt_report`
        #: reads it as a real measurement.
        _calls_at_start = (_COUNTER.calls_rest, _COUNTER.calls_graphql)
        #: harmonic-forge#638 AC1: whether THIS cycle emitted any stdout
        #: line at all, across every source below (queue-for/sweep-for,
        #: comment-watch, branch-ahead). Drives `quiet_streak`.
        cycle_emitted = False

        # Re-enumerated every cycle, not frozen at arm time: a Monitor lives
        # for the whole session, and Lane 2 creates worktrees during it
        # (harmonic-forge#590 preclose finding). Every other discovery step in
        # this loop is already re-derived; this one now is too.
        try:
            args.worktrees = current_worktrees()
        except RootNotARepo as exc:
            # A root that vanished mid-session (deleted, unmounted) is a reason
            # to shout and keep watching the set we had -- not to die. A dead
            # belt is the one failure this protocol cannot tolerate, and
            # `parser.error` at arm time already caught the typo case.
            print(f"[watch_lane_posts] {exc} -- keeping the previous worktree set",
                  file=sys.stderr)
        resolutions = drop_closed_targets(
            [(path, *resolve_worktree(path)) for path in args.worktrees])
        discovered = {pair for _, pair, _ in resolutions if pair is not None}
        if discovered != last_discovered:
            print(f"[watch_lane_posts] now watching {sorted(discovered | static_pairs)}",
                  file=sys.stderr)
            _report_resolutions(resolutions)
            last_discovered = discovered

        mode = args.queue_for or args.sweep_for
        queue: dict = last_queue
        ok_repos: set[str] = set()
        if mode:
            label = "sweep-for" if args.sweep_for else "queue-for"
            print(f"[watch_lane_posts] {label}-{mode} scanning "
                  f"{len(repos)} repo(s):", file=sys.stderr)
            #: harmonic-forge#686/#691 (rescoped). The candidate set is what
            #: this belt already holds — issues its worktrees currently
            #: name, any `--repo/--issues` handed to it, and any
            #: `(repo, issue)` any of the three marker-posting tools has
            #: recently recorded AND whose newest entry is queue-eligible
            #: for THIS `mode` (harmonic-forge#691 AC2' — the no-worktree-yet
            #: replacement for #686's removed scan, kind/poster-filtered so
            #: the set stays at single digits per tick) — never a scan.
            #: `discovered` is re-derived from live branches every cycle
            #: just above, so a worktree that switches branches changes the
            #: set without any GitHub call; `read_queue_candidates` is
            #: likewise a local file read, never a GitHub call.
            posted_candidates = read_queue_candidates(repos, mode)
            queue, lines, ok_repos = queue_cycle(
                repos, mode, last_queue, l1_since, now,
                sweep=bool(args.sweep_for),
                candidate_pairs=discovered | static_pairs | posted_candidates)
            if first_queue_report:
                # Reports repos that ACTUALLY REPORTED, not len(argv). A run
                # where every search failed used to print a line byte-identical
                # to two genuinely quiet repos (harmonic-forge#596 preclose).
                print(f"[watch_lane_posts] {label}-{mode}: {len(queue)} "
                      f"issue(s) queued now across {len(ok_repos)}/{len(repos)} "
                      f"repo(s) that reported", file=sys.stderr)
                first_queue_report = False
            for line in lines:
                print(line)
                sys.stdout.flush()
                cycle_emitted = True
            _record_line_refs(tick, lines)
            last_queue = queue

        # harmonic-forge#599. `SKILL.md` declares dedup as one mechanic with
        # three parts and says "Do not simplify it back"; this path had none of
        # them -- one in-memory `since`, advanced unconditionally after a fetch
        # that swallowed failures to `[]`. A rate limit therefore lost that
        # window permanently and silently, which is the failure the mechanic
        # exists to prevent, in the file that documents it as mandatory.
        #
        comment_lines, comment_fetch_failed = comment_watch_cycle(
            sorted(discovered | static_pairs), watch,
            now, watermarks, seen, primed_targets, tick)
        for line in comment_lines:
            print(line)
            sys.stdout.flush()
            cycle_emitted = True
        since = now

        #: harmonic-forge#583 AC4 preclose finding: this was gated on `"l2"
        #: in watch`, which reads as "surface *other* lanes' Lane 2 posts to
        #: me" -- the lane that actually owns a worktree and could be the
        #: one silently ahead of `origin/main` (Lane 2, per its own
        #: `belt-and-suspenders` command, `--worktrees ... --watch l1`) by
        #: definition never passes `--watch l2` for itself. `branch_ahead_
        #: lines` runs for every resolved worktree unconditionally.
        #: harmonic-forge#590 amends the second half of this note: it used to
        #: say Lane 1's belt has no `--worktrees` and so this is a no-op
        #: there. Lane 1's belt is now worktrees-first, so it is NOT a no-op,
        #: and the abandoned checkouts of closed issues would report as
        #: permanently "ahead" (main squash-merged their work). That is why
        #: `resolutions` is filtered through `drop_closed_targets` above --
        #: the filter, not an empty list, is what keeps this honest now.
        ahead_lines = branch_ahead_lines(resolutions, last_ahead)
        for line in ahead_lines:
            print(line)
            sys.stdout.flush()
            cycle_emitted = True
        _record_line_refs(tick, ahead_lines)

        # harmonic-forge#638 AC1/AC2/AC3: back off on sustained quiet, reset
        # the moment anything is found -- or the moment anything is merely
        # KNOWN to still be outstanding (`cycle_is_quiet`, preclose findings
        # 1/2: a still-queued unchanged item and a failed fetch both print
        # nothing, and neither may read as "nothing found"). `next_poll_
        # interval` cannot return anything but a positive number of seconds
        # (AC2) -- there is no branch here that skips the sleep-and-continue.
        #: harmonic-forge#685. Written before the sleep, unconditionally —
        #: including on a quiet tick, which is the case the record exists for.
        #: `repo_result` distinguishes "polled and matched nothing" from "never
        #: polled", so a blind poll cannot read as a quiet one in the log the
        #: same way `N/M repo(s) that reported` already keeps it honest on
        #: stderr.
        quiet = cycle_is_quiet(cycle_emitted, queue, mode, ok_repos, repos,
                                comment_fetch_failed)

        #: harmonic-forge#685. Written AFTER `cycle_is_quiet`, deliberately.
        #: An earlier draft wrote before it and labelled the tick from
        #: `cycle_emitted` alone — so a tick whose comment fetches had all
        #: failed (`comment_fetch_failed`) printed nothing and was recorded
        #: byte-identically to a genuinely quiet one. `cycle_is_quiet`'s own
        #: docstring is that "I do not know" must never read as "nothing
        #: found", and computing it nine lines after the record was already on
        #: disk defeated exactly that.
        if mode:
            #: harmonic-forge#686 preclose finding 3. `ok_repos` is only ever
            #: populated inside `if mode:` above -- a comment-watch-only run
            #: (no `--queue-for`/`--sweep-for`, e.g. `--repo X --issues N
            #: --watch l1`) never touches it, so recording this loop
            #: unconditionally wrote `ok=False` for every repo on every tick
            #: of a belt that made zero failing calls: `repos` is populated
            #: from `--repo`/`--account-repos` independent of `mode`, but
            #: "reported" is a queue-cycle-only concept. `belt_report` then
            #: counted those as `errored` and printed a failure count against
            #: a repo that never failed anything -- manufactured telemetry,
            #: the false-confidence failure in the other direction.
            for _r in repos:
                #: BARE repo: `belt_report` labels rows `<account>/<repo>`,
                #: so passing the owner-qualified form yields
                #: `vitalharmony/vitalharmony/hrse` and matches no ref's
                #: repo half.
                tick.repo_result(_ACCOUNT, _r.rsplit("/", 1)[-1], _r in ok_repos)
        tick.counter = CallCounter(
            calls_rest=_COUNTER.calls_rest - _calls_at_start[0],
            calls_graphql=_COUNTER.calls_graphql - _calls_at_start[1],
        )
        if comment_fetch_failed:
            tick.actions_taken.append("comment-fetch-failed")
        #: harmonic-forge#686 preclose finding (related to 4): this used to
        #: read `cycle_emitted` directly, the same "I do not know" vs
        #: "nothing found" conflation `cycle_is_quiet` exists to fix -- a
        #: tick with a non-empty unchanged queue, or an under-reporting
        #: repo, prints nothing (`cycle_emitted=False`) but is not quiet.
        #: `quiet`, computed just above, is the one value that already
        #: accounts for all of that; label from it, not from the raw print
        #: flag it was derived to replace.
        tick.actions_taken.append("quiet" if quiet else "emitted")
        try:
            tick.write()
        except OSError as exc:
            #: A telemetry write must never kill the belt — a dead belt is the
            #: one failure this protocol cannot tolerate (DESIGN.md).
            print(f"[watch_lane_posts] tick log write failed: {exc}",
                  file=sys.stderr)
        quiet_streak = 0 if not quiet else quiet_streak + 1
        polls_this_window += 1
        sleep_for = sleep_before_next_poll(
            args.interval, quiet_streak, deadline, _monotonic())
        if sleep_for is None:
            # harmonic-forge#680 AC3: the MEASURED count, printed at the one
            # moment it is known. A number derived from the schedule is the
            # same species of claim as the 3000s cap that was never reachable.
            print(f"[watch_lane_posts] window complete: {polls_this_window} poll(s) "
                  f"in {args.deadline_seconds}s (base interval {args.interval}s). "
                  "Re-arm to continue.", file=sys.stderr)
            return 0
        if sleep_for != last_reported_interval:
            print(f"[watch_lane_posts] next poll in {sleep_for}s "
                  f"(base {args.interval}s, quiet_streak={quiet_streak})",
                  file=sys.stderr)
            last_reported_interval = sleep_for
        time.sleep(sleep_for)


if __name__ == "__main__":
    raise SystemExit(main())
