#!/usr/bin/env python3
"""Repo hygiene backstop — orphaned branches, stranded work, stale worktrees.

hrse#808. Nothing systematically detected any of this. A manual sweep on
2026-08-12 removed 108 stale branches and 6 orphaned worktrees that no existing
check had ever surfaced, and found one repo carrying 295 branches and 255
auto-generated PRs of which 3 had ever been merged.

DESIGN NOTES, because each is a decision that could reasonably have gone the
other way:

1. REPORT-ONLY. Never deletes. Of 42 branches that looked stale by every
   automatic signal in that sweep, 12 held unique commits and still needed a
   human judgment call. Auto-deletion would have destroyed work.

2. STRANDED fails the check; ORPHANED does not. A check that always passes is
   worthless, and a check that is red by default gets ignored — both failure
   modes were observed live the same day. So the exit code tracks the one
   category that means "you may be losing work", and cleanup opportunities are
   reported without failing.

3. git + REST only, never GraphQL. The GraphQL budget is shared with CI: on
   2026-08-12 a single board sync consumed 83% of the hourly quota and reddened
   three consecutive main builds (hrse#814). A hygiene check must never be able
   to do that. REST carries its own separate 5,000/hr budget.

4. Repo-parameterized, no hardcoded owner. Client repos (kenekted, leasepal,
   akcelita) sit on separate GitHub accounts and get their own instances of this
   tooling from harmonic-forge — they must reuse this file rather than fork it.
"""

import argparse
import re
from datetime import datetime, timedelta, timezone
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


class GhError(Exception):
    """A gh/git call failed. Never fail open silently — a hygiene check that
    quietly reports 'all clean' because its API calls failed is worse than one
    that errors loudly."""


def _run(args: list[str], cwd: str | None = None) -> str:
    result = subprocess.run(args, capture_output=True, text=True, cwd=cwd)
    if result.returncode != 0:
        raise GhError(f"{' '.join(args[:3])}…: {result.stderr.strip()[:200]}")
    return result.stdout


def _rest(path: str) -> list[dict]:
    """Paginated REST GET. `--paginate` matters: gh's implicit per-page default
    silently truncates at 30 on repos with hundreds of branches (the same class
    of bug as hrse#800's 500-item board truncation)."""
    out = _run(["gh", "api", path, "--paginate", "-H", "Accept: application/vnd.github+json"])
    items: list[dict] = []
    decoder = json.JSONDecoder()
    idx = 0
    text = out.strip()
    while idx < len(text):  # --paginate concatenates JSON arrays
        obj, end = decoder.raw_decode(text, idx)
        items.extend(obj if isinstance(obj, list) else [obj])
        idx = end
        while idx < len(text) and text[idx] in " \n\r\t":
            idx += 1
    return items


@dataclass
class Finding:
    repo: str
    name: str
    detail: str


@dataclass
class Report:
    orphaned: list[Finding] = field(default_factory=list)   # safe to delete
    stranded: list[Finding] = field(default_factory=list)   # may hold real work
    worktrees: list[Finding] = field(default_factory=list)
    unrun_migrations: list[Finding] = field(default_factory=list)
    unlabelled_migrations: list[Finding] = field(default_factory=list)
    unboarded: list[Finding] = field(default_factory=list)
    checkout_off_main: list[Finding] = field(default_factory=list)
    stale_stashes: list[Finding] = field(default_factory=list)
    missing_transaction_log: list[Finding] = field(default_factory=list)

    @property
    def actionable(self) -> bool:
        # unlabelled_migrations is reported but does NOT fail, same as
        # orphaned. Eight genuine findings surfaced on the first 30-day
        # run (a ninth was a mentioned-not-owned ref), so failing on them would leave this permanently red until
        # someone backfills labels -- which is exactly how hrse#808 says
        # a check gets ignored. The *unrun* case still fails: that one is
        # an incident, not a backlog.
        # unboarded is reported and does NOT fail, same as orphaned. An
        # issue off a board, or on one with a field unset, is a cleanup
        # opportunity rather than lost work -- hrse#979 scope item 3 is
        # explicit that this must not change the exit code.
        # checkout_off_main, stale_stashes and missing_transaction_log
        # (hrse#808) are report-only too -- nothing in the issue asked for a
        # new failing condition, and each is a "go look at this", not a
        # "you may be losing work" the way STRANDED is.
        return bool(self.stranded or self.unrun_migrations)


def audit_repo(repo: str, report: Report) -> None:
    """Classify every branch in `repo`. One REST call per branch only for the
    ambiguous ones, so cost scales with genuine ambiguity rather than repo size."""
    # Not hardcoded to "main": client repos this gets reused on may differ.
    default = json.loads(
        _run(["gh", "api", f"repos/{repo}", "--jq", "{d: .default_branch}"])
    )["d"]

    branches = [b["name"] for b in _rest(f"repos/{repo}/branches?per_page=100")]
    prs = _rest(f"repos/{repo}/pulls?state=all&per_page=100")

    merged: set[str] = set()
    open_: set[str] = set()
    closed_unmerged: set[str] = set()
    for pr in prs:
        ref = pr["head"]["ref"]
        if pr.get("merged_at"):
            merged.add(ref)
        elif pr.get("state") == "open":
            open_.add(ref)
        else:
            closed_unmerged.add(ref)

    for branch in branches:
        if branch == default or branch in open_:
            continue
        if branch in merged:
            report.orphaned.append(Finding(repo, branch, "PR merged"))
            continue

        # No open PR and not merged — does it carry work that exists nowhere else?
        try:
            cmp = json.loads(_run([
                "gh", "api",
                f"repos/{repo}/compare/{default}...{branch}",
                "--jq", "{ahead: .ahead_by}",
            ]))
            ahead = cmp["ahead"]
        except GhError as exc:
            report.stranded.append(Finding(repo, branch, f"could not compare: {exc}"))
            continue

        if ahead == 0:
            report.orphaned.append(Finding(repo, branch, f"fully contained in {default}"))
        elif branch in closed_unmerged:
            report.stranded.append(
                Finding(repo, branch, f"{ahead} commit(s) ahead, PR closed WITHOUT merging"))
        else:
            # Known and accepted: this also catches work genuinely in progress
            # (a lane mid-implementation has commits and no PR yet). That is a
            # true positive — the work does exist nowhere but that branch — and
            # deliberately not filtered by age, because "old enough to worry
            # about" is exactly the judgment this refuses to make on your
            # behalf. Report-only means a noisy true positive costs a glance.
            report.stranded.append(
                Finding(repo, branch, f"{ahead} commit(s) ahead, no PR ever opened"))


MIGRATION_LABEL = "data-migration"
EXECUTED_LABEL = "migration-executed"
ABANDONED_LABEL = "migration-abandoned"


def audit_migrations(repo: str, report: Report) -> None:
    """Closed data-migration issues with no record that the migration ran.

    hrse#867, and the load-bearing control for the hrse#849 failure: that
    issue's code fix merged, the issue closed, and all 219 target rows
    were still null. It blocked two downstream issues for days and was
    caught only because a human noticed in conversation.

    hrse#859's PreToolUse hook guards the same invariant at close time,
    but it is fail-open and sees only closes issued through the Bash
    tool -- not `gh api graphql`, not heredoc bodies, not the web UI.
    This sweep reads state afterwards, so it catches every close *path*
    regardless of how the close happened.

    **It does not catch an unlabelled migration, and would not have
    caught hrse#849 as it actually happened.** Verified from that
    issue's timeline: `data-migration` was applied at 00:34, 64 minutes
    AFTER the close -- retroactively, during incident response. At close
    time it carried only `tech-debt`, so a sweep running that night
    would have reported nothing.

    Both this sweep and hrse#859's hook key on the same label, so both
    inherit one unenforced dependency: a human applying `data-migration`
    at filing time. The founding incident is itself proof that
    discipline fails there. Tracked as hrse#871 --
    stated here rather than left implicit, because a control whose
    docstring overstates its coverage is how the next one goes
    unnoticed.

    Reads the label set only. No parsing of comment prose: four review
    rounds on hrse#859 established that a published marker format makes
    every published example a valid credential.
    """
    issues = _rest(
        f"repos/{repo}/issues?state=closed&labels={MIGRATION_LABEL}&per_page=100"
    )
    for issue in issues:
        if "pull_request" in issue:  # the issues endpoint returns PRs too
            continue
        labels = {label["name"] for label in issue.get("labels", [])}
        if labels & {EXECUTED_LABEL, ABANDONED_LABEL}:
            continue
        closed_at = (issue.get("closed_at") or "unknown")[:10]
        closed_by = (issue.get("closed_by") or {}).get("login", "unknown")
        title = issue["title"]
        title = title if len(title) <= 80 else title[:79] + "…"
        report.unrun_migrations.append(Finding(
            repo=repo,
            name=f"#{issue['number']}",
            detail=(f"closed {closed_at} by {closed_by} — no "
                    f"{EXECUTED_LABEL!r} or {ABANDONED_LABEL!r} label; "
                    f"{issue['title'][:60]}"),
        ))


# CLAUDE.md convention: single-use migration scripts carry a numeric
# prefix. A commit touching one is a mechanical signal that a migration
# shipped -- unlike issue prose, it cannot be reworded away.
MIGRATION_PATH = re.compile(r"(?:^|/)scripts/[123]-")

# Not every numbered script mutates data. Diagnostics, gates and
# environment setup share the prefix; flagging them turns this into a
# check that is red by default, which is how hrse#808 says a check gets
# ignored. Same exemption vocabulary as the stale-script guard.
# Exempted by name. Note this is a *heuristic on filenames*, not a claim
# that these change no data -- 1-gate_450_direction.py does contain MERGE
# and DETACH DELETE, and 1-setup_keycloak_realm.py writes Keycloak config.
# They are exempt because they are not graph migrations owned by an
# issue, which is what this control is about.
NON_MUTATING_SCRIPT = re.compile(
    r"(?:^|/)scripts/[123]-(?:verify|diagnos|check|audit|report|inspect"
    r"|gate|setup|install|retrigger)")

# Squash subjects reference issues two ways in this corpus: "(#849)" and
# the "H849" shorthand. Missing the second hid 100% of the H-referenced
# population -- an 18% live miss rate in a 30-day window, and silent.
# Group 1 captures an optional repo prefix so a cross-repo "forge#266"
# is not looked up as this repo's #266, which exists and would flag an
# unrelated issue.
ISSUE_REF = re.compile(r"(?:\b([\w.-]+))?#(\d+)\b|\bH(\d+)\b")


def _refs_in(subject: str, repo: str) -> set[str]:
    """Issue numbers in a squash subject, excluding other repos' refs."""
    short = repo.split("/")[-1]
    numbers: set[str] = set()
    for prefix, hashed, h_form in ISSUE_REF.findall(subject):
        if h_form:
            numbers.add(h_form)
        elif hashed and (not prefix or prefix in (short, repo)):
            numbers.add(hashed)
    return numbers

MIGRATION_LOOKBACK_DAYS = 30


def audit_unlabelled_migrations(repo: str, report: Report) -> None:
    """Merged migration commits whose issue never got `data-migration`.

    hrse#871. Both hrse#859's close-time hook and hrse#867's sweep key
    on that label, and nothing enforced it at filing time -- so an
    unlabelled migration was invisible to both. hrse#849 is the proof:
    its timeline shows the label applied 64 minutes AFTER the close,
    retroactively during incident response, meaning neither control
    would have caught the incident that produced them.

    Deliberately keyed on **file paths, not issue prose**. Pattern
    matching migration vocabulary in an issue body is the shape that
    cost hrse#859 four review rounds; a commit that touches
    `scripts/[123]-*` is mechanical and cannot be reworded away.

    Cost, measured live rather than estimated: a 30-day window on this
    corpus returns ~75 commits, ~59 of which carry a ref and therefore
    take a per-commit call for their paths, plus ~11 issue lookups --
    **roughly 70 REST calls and ~50 seconds**. The `path=scripts` filter
    is not name-filtered, so most of those detail calls are on commits
    that turn out to touch nothing numbered. Linear in a busier month,
    and far inside the REST budget; the interactive latency is the real
    cost, not the quota.

    Coverage limits, stated rather than implied:

    * **Issue attribution is still prose.** The path signal is
      mechanical, but refs are parsed from the commit *subject*, which
      humans write. A subject that merely *mentions* an issue can flag
      it (hrse#708 is a live example -- "pre-#708 bug" in a commit
      owned by hrse#719), and a migration whose subject carries no ref
      at all is invisible. 24 of 60 days of subjects carry no ref.
    * **Only root `scripts/[123]-*` is in scope.** Unprefixed mutators
      such as `migrate_communication_channels.py` (21 write statements)
      are not covered. Widening to "any script containing write Cypher"
      would reintroduce content heuristics, which is the shape this
      control exists to avoid.
    * **A migration pushed directly, without a squash-merged PR
      subject, is invisible.**

    Deliberately NOT restricted to files with `status == "added"`, which
    would suppress refactor noise but also miss a fixed-and-re-run
    migration -- hrse#849 itself *modified* an existing script. Since
    this finding is report-only, a false positive costs a glance and a
    false negative costs the whole point.
    """
    since = (datetime.now(timezone.utc)
             - timedelta(days=MIGRATION_LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    commits = _rest(
        f"repos/{repo}/commits?path=scripts&since={since}&per_page=100"
    )

    seen: set[str] = set()
    for commit in commits:
        sha = commit.get("sha", "")
        subject = (commit.get("commit", {}).get("message") or "").split("\n")[0]
        refs = _refs_in(subject, repo)
        if not refs:
            continue

        detail = _rest(f"repos/{repo}/commits/{sha}")
        files = detail[0].get("files", []) if detail else []
        touched = sorted({
            f["filename"] for f in files
            if MIGRATION_PATH.search(f.get("filename", ""))
            and not NON_MUTATING_SCRIPT.search(f.get("filename", ""))
        })
        if not touched:
            continue
        for number in sorted(refs, key=int):
            if number in seen:
                continue
            seen.add(number)
            issue = _rest(f"repos/{repo}/issues/{number}")
            if not issue:
                continue
            issue = issue[0]
            if "pull_request" in issue:  # the ref was the PR, not the issue
                continue
            labels = {label["name"] for label in issue.get("labels", [])}
            if MIGRATION_LABEL in labels:
                continue
            report.unlabelled_migrations.append(Finding(
                repo=repo,
                name=f"#{number}",
                detail=(f"{sha[:7]} touched {', '.join(touched)} but the issue "
                        f"has no {MIGRATION_LABEL!r} label — invisible to both "
                        f"the close gate and the unrun sweep"),
            ))


# hrse#979: which board a repo's issues live on. cymagraph-infra and
# openclaw-projects have no board of their own -- their items sit on board #1
# alongside hrse's. Mirrors gh_issue.py's REPO_BOARDS (harmonic-forge#107);
# kept local rather than imported so this script stays standalone.
_REPO_BOARDS: dict[str, tuple[str, str]] = {
    "vitalharmony/hrse": ("vitalharmony", "1"),
    "vitalharmony/harmonic-forge": ("vitalharmony", "3"),
    "vitalharmony/cymagraph-infra": ("vitalharmony", "1"),
    "vitalharmony/openclaw-projects": ("vitalharmony", "1"),
}

# Board fields every open issue is expected to carry (hrse#966). Milestone is
# deliberately NOT here: a repo carries release milestones only when its work
# ships inside one venture's release, so harmonic-forge having none is a
# recorded decision, not a gap (hrse#979 scope item 4).
_REQUIRED_BOARD_FIELDS = ("Theme", "Venture")

# GraphQL's `projectV2` owner is not polymorphic -- `user` and `organization`
# are separate query roots, so a caller must know which one an owner is.
# hrse#991 comment (2026-08-17): the owner *name* was parameterized here but
# the owner *type* was not, silently breaking this issue's own reuse goal
# for any client-repo instance (kenekted, leasepal, akcelita) that turns out
# to be org-owned rather than user-owned. Per hrse#808's handoff: try
# organization first, fall back to user -- `vitalharmony` itself is
# confirmed a User account (`gh api users/vitalharmony` -> "User"; `gh api
# orgs/vitalharmony` -> 404), so every call against it today costs one
# wasted organization lookup before falling back to the user query that
# actually resolves; disclosed as a known cost of this ordering, not fixed
# here, since the handoff states the try/fallback order as settled rather
# than delegated. Fixed in-place inside this same paginated query rather
# than switching to `gh project --owner`: that CLI path silently truncates
# on large boards (hrse#800) and costs hundreds of GraphQL complexity points
# per full fetch (item_list_cache.py:158-169) -- exactly the quota-burn
# failure mode this cursor-paginated query exists to avoid, independent of
# whether the CLI supports both owner types.
_BOARD_ITEMS_QUERY = """
query($owner: String!, $number: Int!, $cursor: String) {
  user(login: $owner) {
    projectV2(number: $number) {
      items(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          content { ... on Issue { number state repository { nameWithOwner } } }
          theme: fieldValueByName(name: "Theme") {
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
          venture: fieldValueByName(name: "Venture") {
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
        }
      }
    }
  }
}
"""

_BOARD_ITEMS_QUERY_ORG = _BOARD_ITEMS_QUERY.replace(
    "user(login: $owner)", "organization(login: $owner)")


def _run_board_query(query: str, owner: str, number: str, cursor: str | None) -> dict:
    args = ["gh", "api", "graphql", "-f", f"query={query}",
            "-F", f"owner={owner}", "-F", f"number={int(number)}"]
    if cursor:
        args += ["-F", f"cursor={cursor}"]
    payload = json.loads(_run(args))
    if payload.get("errors"):
        raise GhError("; ".join(e.get("message", "?") for e in payload["errors"]))
    return payload


def _board_state(owner: str, number: str) -> dict[tuple[str, int], dict]:
    """(repo, issue) -> {field: value|None} for every OPEN item on a board.

    Owner type is resolved once, on the first page, then reused for every
    later cursor -- a project board's owner cannot change mid-pagination.
    """
    state: dict[tuple[str, int], dict] = {}
    cursor: str | None = None
    owner_field: str | None = None
    while True:
        if owner_field is None:
            # Confirmed live (hrse#808): `gh api graphql` exits NONZERO for
            # `organization(login: $owner)` against a user-owned login --
            # "Could not resolve to an Organization with the login of
            # '...'" -- it does not degrade to a graceful `null` the way a
            # merely-empty/nonexistent project does. The organization
            # attempt's own GhError must be caught here and treated as
            # "try the other type", not left to propagate: letting it
            # propagate was an earlier version of this fix, and it silently
            # skipped `audit_unboarded` (bundled with the migration sweep in
            # `main()`'s shared try/except) for every repo, every run.
            try:
                payload = _run_board_query(_BOARD_ITEMS_QUERY_ORG, owner, number, cursor)
                project = ((payload.get("data") or {}).get("organization") or {}).get("projectV2")
            except GhError:
                project = None
            if project:
                owner_field = "organization"
            else:
                payload = _run_board_query(_BOARD_ITEMS_QUERY, owner, number, cursor)
                project = ((payload.get("data") or {}).get("user") or {}).get("projectV2")
                if not project:
                    raise GhError(f"no project #{number} for {owner} (tried organization and user)")
                owner_field = "user"
        else:
            query = _BOARD_ITEMS_QUERY_ORG if owner_field == "organization" else _BOARD_ITEMS_QUERY
            payload = _run_board_query(query, owner, number, cursor)
            project = ((payload.get("data") or {}).get(owner_field) or {}).get("projectV2")
            if not project:
                raise GhError(f"no project #{number} for {owner}")
        items = project["items"]
        for node in items["nodes"]:
            content = node.get("content") or {}
            if content.get("state") != "OPEN" or not content.get("number"):
                continue
            key = ((content.get("repository") or {}).get("nameWithOwner", ""),
                   content["number"])
            state[key] = {
                "Theme": (node.get("theme") or {}).get("name"),
                "Venture": (node.get("venture") or {}).get("name"),
            }
        if not items["pageInfo"]["hasNextPage"]:
            return state
        cursor = items["pageInfo"]["endCursor"]


def audit_unboarded(repo: str, report: Report, _cache: dict = {}) -> None:
    """Open issues on no board, and boarded ones missing Theme/Venture.

    hrse#979. An unboarded issue has no Theme, no Venture and no Sequence,
    because those are board fields -- so it appears in no board-driven query,
    no capability slice and no burn-up. Two live instances were found only
    because someone happened to run a cross-repo count by hand.

    Reports; never fails the run (scope item 3).
    """
    board = _REPO_BOARDS.get(repo)
    if board is None:
        report.unboarded.append(Finding(
            repo, "(repo)", "no board mapped for this repo — add it to _REPO_BOARDS"))
        return
    owner, number = board
    if board not in _cache:  # one fetch per board, not per repo sharing it
        _cache[board] = _board_state(owner, number)
    state = _cache[board]

    for issue in _rest(f"repos/{repo}/issues?state=open&per_page=100"):
        # `is not None`, not truthiness: the REST payload's pull_request
        # object is populated in practice, but an empty one would be falsy
        # and a PR would be reported as an unboarded issue.
        if issue.get("pull_request") is not None:
            continue
        num = issue["number"]
        fields = state.get((repo, num))
        if fields is None:
            report.unboarded.append(Finding(
                repo, f"#{num}", f"on no board — {issue['title'][:60]}"))
            continue
        missing = [f for f in _REQUIRED_BOARD_FIELDS if not fields.get(f)]
        if missing:
            report.unboarded.append(Finding(
                repo, f"#{num}",
                f"boarded but {' and '.join(missing)} unset — {issue['title'][:50]}"))


def audit_worktrees(checkout: str, report: Report) -> None:
    """Flag worktrees whose branch is gone or already merged. Purely local."""
    try:
        raw = _run(["git", "worktree", "list", "--porcelain"], cwd=checkout)
    except GhError as exc:
        report.worktrees.append(Finding(checkout, "-", f"could not list worktrees: {exc}"))
        return

    path = None
    for line in raw.splitlines():
        if line.startswith("worktree "):
            path = line.split(" ", 1)[1]
        elif line.startswith("branch ") and path:
            branch = line.split("refs/heads/", 1)[-1]
            if path.rstrip("/") == checkout.rstrip("/"):
                path = None
                continue
            try:
                exists = _run(["git", "ls-remote", "--heads", "origin", branch], cwd=checkout).strip()
            except GhError:
                exists = "?"
            if not exists:
                report.worktrees.append(
                    Finding(checkout, path, f"branch '{branch}' no longer on origin"))
            path = None


def audit_checkout_branch(checkout: str, report: Report) -> None:
    """Flag a local checkout that is not sitting on `main`. Purely local.

    hrse#808 comment (2026-08-15): the standing rule is that all work happens
    in dedicated worktrees, which means the *shared* main checkout should
    always be on `main`. A session that assumes otherwise and edits
    docs/PRIORITIES.md on the wrong branch caused an unrecoverable incident
    (hrse#277) -- another session's uncommitted work was interleaved in the
    same working tree, so `git checkout --` would have destroyed it.

    Hardcoded to "main", not the repo's actual default branch (unlike
    `audit_repo`, which reads it via the API) -- this checks a specific local
    invariant about how this checkout is meant to be used, not a
    branch-naming question, and both checkouts this runs against are
    confirmed named "main".
    """
    try:
        branch = _run(["git", "branch", "--show-current"], cwd=checkout).strip()
    except GhError as exc:
        report.checkout_off_main.append(Finding(checkout, "-", f"could not read branch: {exc}"))
        return
    if branch != "main":
        report.checkout_off_main.append(Finding(
            checkout, branch or "(detached HEAD)",
            f"expected 'main', found {branch or '(detached HEAD)'}"))


def audit_stashes(checkout: str, report: Report) -> None:
    """Flag every stash in a local checkout. Purely local, purely reported.

    hrse#808 comment (2026-08-15): a stash is stranded work with LESS
    visibility than a branch -- it sits on no branch at all and never
    appears in `git log`, so none of this file's other checks see it.
    Never auto-dropped: a stash may be another session's live
    work-in-progress, same posture as every other finding in this file.
    """
    try:
        raw = _run(["git", "stash", "list"], cwd=checkout)
    except GhError as exc:
        report.stale_stashes.append(Finding(checkout, "-", f"could not list stashes: {exc}"))
        return
    for line in raw.splitlines():
        if not line.strip():
            continue
        ref, _, description = line.partition(": ")
        report.stale_stashes.append(Finding(checkout, ref, description))


TRANSACTION_LOG_LOOKBACK_DAYS = 30


def audit_transaction_log(checkout: str, report: Report) -> None:
    """Merged commits on `main` that never touched transaction-log.md and
    whose own headline was never backfilled into it either.

    hrse#808 comment (2026-08-12): the log is only written by
    `mise run commit`/`restart`, so work that lands through a squash-merged
    PR -- now most work -- silently skips it. Ten merges on one day left no
    trace until backfilled by hand.

    **Two-signal design, corrected from an initial single-signal version
    after it produced 524/588 false positives on a live run against hrse.**
    The first version only compared a merged commit's own subject against
    documented headlines, on the assumption "headline = verbatim commit
    message" (the file's own header) meant a 1:1 match. It doesn't, in
    practice: `mise run commit` writes one entry per LOCAL commit, keyed to
    THAT commit's own message. A multi-commit PR's local commits each
    self-document under their own headline, but GitHub's squash-merge
    collapses them into one remote commit whose final subject is the PR
    title -- text that was never any local commit's message and so never
    matches any entry, even though the work genuinely WAS logged. Confirmed
    live: commit `c175e5e` (subject "Career 2.7-A: rank Discovery queue...")
    diffs transaction-log.md adding an entry headlined "## build: Auto-bump
    to v2.6.83..." -- a different string, from a bundled local cycle.

    A commit now counts as documented if EITHER (a) its own diff touched
    transaction-log.md at all -- the direct signal that `mise run commit`/
    `restart` ran somewhere in this PR's lifecycle, regardless of which
    local headline it wrote -- or (b) some commit's diff (its own or a
    later one) added a `## <headline>` line matching this commit's own
    subject exactly -- the manual-backfill case, hrse#817's actual
    precedent: a later, separate commit added entries headlined with each
    original merge's own subject, and neither of those original merges nor
    the backfill commit touched the file in a way (a) would catch for them
    individually in every case, so (b) stays as a second, independent path.

    Local and checkout-scoped, not REST-based, by design: this repo's own
    transaction-log.md has 249 commits in its history as of 2026-08-18;
    reconstructing that via the REST API would cost one detail call per
    commit (the same shape as `audit_unlabelled_migrations`'s per-commit
    lookups), where a single local `git log -p`/`--name-only` pair is two
    subprocess calls total, regardless of history size.

    Gated on the file actually existing in this checkout: cymagraph-infra
    and openclaw-projects carry no transaction-log.md at all (confirmed
    live, 404 on both), so this silently does nothing there rather than
    flagging every merge permanently -- exactly the "check nobody can turn
    green" anti-pattern `Report.actionable`'s design note warns against.

    Walks the file's **full history** (`git log -p`), not its current
    content, because the header states the file is cleared (on a version
    bump for hrse; on push to main for harmonic-forge -- confirmed each
    repo states its own clearing trigger in its own file header, both
    handled identically here since both mean "current content undercounts
    history"). Grepping current content would false-positive on every merge
    older than the last clear -- its entry is gone from the live file by
    design, not because it was skipped.
    """
    if not (Path(checkout) / "transaction-log.md").is_file():
        return

    try:
        _run(["git", "fetch", "origin", "main"], cwd=checkout)
    except GhError:
        pass  # best-effort freshness; proceed against whatever origin/main we have

    since = (datetime.now(timezone.utc)
             - timedelta(days=TRANSACTION_LOG_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    try:
        recent = _run(
            ["git", "log", "origin/main", f"--since={since}",
             "--name-only", "--pretty=format:===%H===%s"], cwd=checkout)
        history = _run(["git", "log", "-p", "--", "transaction-log.md"], cwd=checkout)
    except GhError as exc:
        report.missing_transaction_log.append(
            Finding(checkout, "-", f"could not walk history: {exc}"))
        return

    documented_headlines: set[str] = set()
    for line in history.splitlines():
        if line.startswith("+## "):
            documented_headlines.add(line[len("+## "):].strip())

    flagged: set[str] = set()
    subject: str | None = None
    touched_log = False

    def _flush() -> None:
        if subject is None:
            return
        if (subject not in documented_headlines and not touched_log
                and subject not in flagged):
            flagged.add(subject)
            report.missing_transaction_log.append(Finding(
                checkout, subject[:70],
                "merged commit neither touched transaction-log.md nor has a "
                "matching backfilled entry"))

    for raw_line in recent.splitlines():
        if raw_line.startswith("==="):
            _flush()
            _, _, rest = raw_line.partition("===")
            _, _, subj = rest.partition("===")
            subject = subj.strip()
            touched_log = False
        elif raw_line.strip() == "transaction-log.md":
            touched_log = True
    _flush()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--repo", action="append", default=[], metavar="OWNER/NAME",
                        help="repository to audit; repeatable")
    parser.add_argument("--checkout", action="append", default=[], metavar="PATH",
                        help="local checkout whose worktrees to audit; repeatable")
    args = parser.parse_args()

    if not args.repo and not args.checkout:
        parser.error("nothing to audit — pass at least one --repo or --checkout")

    report = Report()
    for repo in args.repo:
        try:
            audit_repo(repo, report)
        except GhError as exc:
            print(f"ERROR auditing {repo}: {exc}", file=sys.stderr)
            return 2
        try:
            audit_migrations(repo, report)
            audit_unlabelled_migrations(repo, report)
            audit_unboarded(repo, report)
        except GhError as exc:
            # A repo with Issues disabled returns 410 here. That is a
            # reason to skip one audit, not to abandon the remaining
            # repos and every --checkout worktree scan.
            print(f"WARN: migration sweep skipped for {repo}: {exc}",
                  file=sys.stderr)
    for checkout in args.checkout:
        audit_worktrees(checkout, report)
        audit_checkout_branch(checkout, report)
        audit_stashes(checkout, report)
        audit_transaction_log(checkout, report)

    if report.unlabelled_migrations:
        print(f"UNLABELLED MIGRATIONS — {len(report.unlabelled_migrations)} "
              f"issue(s) shipped a migration script without the "
              f"{MIGRATION_LABEL!r} label:")
        for f in report.unlabelled_migrations:
            print(f"  {f.repo} [{f.name}] — {f.detail}")
        print("  Applying data-migration to these makes them visible to "
              "hrse#859's close gate and hrse#867's sweep. Check the ref "
              "first — a subject may *mention* an issue rather than own it. "
              "Reported, not failed — see hrse#871.")
        print()
    if report.unboarded:
        print(f"UNBOARDED — {len(report.unboarded)} open issue(s) invisible to "
              f"board-driven reporting:")
        for f in report.unboarded:
            print(f"  {f.repo} [{f.name}] — {f.detail}")
        print("  An issue off a board, or missing Theme/Venture, appears in no "
              "capability slice and no burn-up (hrse#965/#967). Reported, not "
              "failed — it is a cleanup opportunity, not lost work.")
        print()
    if report.unrun_migrations:
        print(f"UNRUN MIGRATIONS — {len(report.unrun_migrations)} closed "
              f"issue(s) with no record the migration ran:")
        for f in report.unrun_migrations:
            print(f"  {f.repo} [{f.name}] — {f.detail}")
        print()
    if report.stranded:
        print(f"STRANDED — {len(report.stranded)} branch(es) may hold work that exists nowhere else:")
        for f in report.stranded:
            print(f"  {f.repo} [{f.name}] — {f.detail}")
        print()
    if report.orphaned:
        print(f"ORPHANED — {len(report.orphaned)} branch(es) safe to delete:")
        for f in report.orphaned:
            print(f"  {f.repo} [{f.name}] — {f.detail}")
        print()
    if report.worktrees:
        print(f"STALE WORKTREES — {len(report.worktrees)}:")
        for f in report.worktrees:
            print(f"  {f.name} — {f.detail}")
        print()
    if report.checkout_off_main:
        print(f"CHECKOUT OFF MAIN — {len(report.checkout_off_main)}:")
        for f in report.checkout_off_main:
            print(f"  {f.repo} — {f.detail}")
        print("  A shared main checkout not on 'main' is exactly the setup for "
              "an interleaved-work incident (hrse#277).")
        print()
    if report.stale_stashes:
        print(f"STALE STASHES — {len(report.stale_stashes)}:")
        for f in report.stale_stashes:
            print(f"  {f.repo} {f.name} — {f.detail}")
        print("  A stash is stranded work with less visibility than a branch — "
              "never on any branch, never in `git log`. Nothing is dropped "
              "automatically.")
        print()
    if report.missing_transaction_log:
        print(f"MISSING TRANSACTION-LOG ENTRIES — {len(report.missing_transaction_log)}:")
        for f in report.missing_transaction_log:
            print(f"  {f.repo} — {f.name} — {f.detail}")
        print("  Work that landed via PR left no trace in transaction-log.md. "
              "See its own header for the sanctioned backfill helper.")
        print()

    if not (report.stranded or report.orphaned or report.worktrees
            or report.unrun_migrations or report.unlabelled_migrations
            or report.unboarded or report.checkout_off_main
            or report.stale_stashes or report.missing_transaction_log):
        print("repo hygiene: clean.")
        return 0

    # Only STRANDED fails. Orphaned branches are a cleanup opportunity, and
    # failing on them would leave this permanently red until someone tidies —
    # which is how a check gets ignored.
    if report.actionable:
        if report.unrun_migrations:
            print("A closed data-migration issue with no execution record means "
                  "either the migration never ran, or the label was missed. "
                  "Check the issue, then apply migration-executed or "
                  "migration-abandoned (hrse#859/#867).")
        if report.stranded:
            print("Review the STRANDED list before deleting anything.")
        print("Nothing was deleted.")
        return 1
    print("Nothing stranded. Nothing was deleted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
