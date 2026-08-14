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
import json
import subprocess
import sys
from dataclasses import dataclass, field


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

    @property
    def actionable(self) -> bool:
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
        except GhError as exc:
            # A repo with Issues disabled returns 410 here. That is a
            # reason to skip one audit, not to abandon the remaining
            # repos and every --checkout worktree scan.
            print(f"WARN: migration sweep skipped for {repo}: {exc}",
                  file=sys.stderr)
    for checkout in args.checkout:
        audit_worktrees(checkout, report)

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

    if not (report.stranded or report.orphaned or report.worktrees
            or report.unrun_migrations):
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
