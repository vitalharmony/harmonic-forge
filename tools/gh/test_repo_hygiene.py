#!/usr/bin/env python3
"""Tests for repo_hygiene.py (hrse#808)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import repo_hygiene as rh


class ClassificationTests(unittest.TestCase):
    """Branch classification. Every case here is one the 2026-08-12 manual
    sweep actually produced."""

    def _audit(self, branches, prs, compares, errors=None, default="main"):
        def fake_run(args, cwd=None):
            joined = " ".join(args)
            if "--jq" in args and "default_branch" in joined:
                return json.dumps({"d": default})
            if "/branches" in joined:
                return json.dumps([{"name": b} for b in branches])
            if "/pulls" in joined:
                return json.dumps(prs)
            if "/compare/" in joined:
                branch = joined.split("...")[1].split()[0]
                if branch in errors:
                    raise rh.GhError(errors[branch])
                return json.dumps({"ahead": compares.get(branch, 0)})
            raise AssertionError(f"unexpected call: {joined}")

        errors = errors or {}
        report = rh.Report()
        with patch.object(rh, "_run", side_effect=fake_run):
            rh.audit_repo("acme/repo", report)
        return report

    def _pr(self, ref, state="closed", merged=True):
        return {"head": {"ref": ref}, "state": state,
                "merged_at": "2026-01-01T00:00:00Z" if merged else None}

    def test_default_branch_is_never_flagged(self):
        r = self._audit(["main"], [], {})
        self.assertEqual(r.orphaned, [])
        self.assertEqual(r.stranded, [])

    def test_branch_with_open_pr_is_active(self):
        r = self._audit(["main", "feat/x"], [self._pr("feat/x", "open", False)], {})
        self.assertEqual(r.orphaned, [])
        self.assertEqual(r.stranded, [])

    def test_merged_pr_branch_is_orphaned(self):
        r = self._audit(["main", "feat/x"], [self._pr("feat/x")], {"feat/x": 1})
        self.assertEqual(len(r.orphaned), 1)
        self.assertEqual(r.stranded, [])

    def test_contained_branch_is_orphaned_even_without_a_pr(self):
        """Squash-merged branches show commits 'ahead' by sha but zero by content."""
        r = self._audit(["main", "feat/x"], [], {"feat/x": 0})
        self.assertEqual(len(r.orphaned), 1)
        self.assertIn("fully contained", r.orphaned[0].detail)

    def test_unique_commits_and_no_pr_is_stranded(self):
        """The case that matters — the harmonic-forge#98 shape.

        Commits ahead, NO PR at all. harmonic-forge#433 considered
        demoting this arm to report-only and rejected it: this is the
        founding incident, and `repo-hygiene.yml` gates its rolling-issue
        step on the exit code, so demoting it means no issue and no
        notification.
        """
        r = self._audit(["main", "feat/x"], [], {"feat/x": 3})
        self.assertEqual(len(r.stranded), 1)
        self.assertIn("no PR ever opened", r.stranded[0].detail)

    def test_closed_unmerged_pr_with_commits_is_stranded(self):
        r = self._audit(["main", "feat/x"], [self._pr("feat/x", "closed", False)], {"feat/x": 2})
        self.assertEqual(len(r.stranded), 1)
        self.assertIn("WITHOUT merging", r.stranded[0].detail)

    def test_non_main_default_branch_respected(self):
        r = self._audit(["trunk", "feat/x"], [], {"feat/x": 0}, default="trunk")
        self.assertEqual([f.name for f in r.orphaned], ["feat/x"])

    def test_compare_failure_is_stranded_not_silently_clean(self):
        """A failed lookup must never read as 'nothing here'."""
        def fake_run(args, cwd=None):
            joined = " ".join(args)
            if "--jq" in args and "default_branch" in joined:
                return json.dumps({"d": "main"})
            if "/branches" in joined:
                return json.dumps([{"name": "main"}, {"name": "feat/x"}])
            if "/pulls" in joined:
                return json.dumps([])
            raise rh.GhError("boom")

        report = rh.Report()
        with patch.object(rh, "_run", side_effect=fake_run):
            rh.audit_repo("acme/repo", report)
        # harmonic-forge#433 AC4 moved this out of STRANDED without letting it
        # go quiet: inability to compare is not evidence of stranding, but a
        # failed lookup must still never read as "nothing here". The intent of
        # this test is unchanged; only the category it lands in moved.
        self.assertEqual(report.stranded, [])
        self.assertEqual(len(report.incomparable), 1)
        self.assertEqual(report.orphaned, [])

    # --- harmonic-forge#433: what STRANDED may contain, and what it may not ---

    def test_no_common_ancestor_is_not_stranded(self):
        """An orphan/data branch (`metrics/board-snapshots`) can never compare.

        Reported forever under a heading asserting possible data loss, it is
        the always-fires inverse of the always-passes check hrse#808 Phase 3
        deleted `narrative_budget_check.py` for.
        """
        r = self._audit(
            ["main", "metrics/board-snapshots"], [], {},
            errors={"metrics/board-snapshots":
                    "No common ancestor between main and metrics/board-snapshots."})
        self.assertEqual(r.stranded, [])
        self.assertEqual(len(r.unrelated_history), 1)
        self.assertEqual(r.incomparable, [])

    def test_generic_compare_failure_is_not_an_orphan(self):
        """Bound to the literal message. A deleted or renamed branch returns
        `Not Found` on the same 404 and must not be classified as deliberate."""
        r = self._audit(["main", "feat/gone"], [], {},
                        errors={"feat/gone": "Not Found (HTTP 404)"})
        self.assertEqual(r.stranded, [])
        self.assertEqual(r.unrelated_history, [])
        self.assertEqual(len(r.incomparable), 1)
        self.assertIn("could not compare", r.incomparable[0].detail)

    def test_no_pr_at_all_still_trips_the_exit_code(self):
        """The regression guard for harmonic-forge#433 finding 1, in both
        directions: this arm must stay in `Report.actionable`."""
        r = self._audit(["main", "feat/x"], [], {"feat/x": 3})
        self.assertEqual(len(r.stranded), 1)
        self.assertTrue(r.actionable)

    def test_neither_new_category_trips_the_exit_code(self):
        r = self._audit(["main", "metrics/snap", "feat/gone"], [], {},
                        errors={"metrics/snap": "No common ancestor between a and b.",
                                "feat/gone": "Not Found (HTTP 404)"})
        self.assertFalse(r.actionable)

class ExitCodeTests(unittest.TestCase):
    """Only STRANDED fails. Both failure modes this guards against were seen
    live on 2026-08-12: a check that always passes, and a check that is red by
    default until someone tidies."""

    def test_actionable_only_when_stranded(self):
        r = rh.Report()
        self.assertFalse(r.actionable)
        r.orphaned.append(rh.Finding("a/b", "x", "PR merged"))
        self.assertFalse(r.actionable, "orphaned cleanup must not fail the check")
        r.stranded.append(rh.Finding("a/b", "y", "2 commits ahead"))
        self.assertTrue(r.actionable)

    def test_unlabelled_migration_reports_but_does_not_fail(self):
        """hrse#871: eight genuine findings on the first run. Failing on a
        backlog is how hrse#808 says a check gets ignored."""
        r = rh.Report()
        r.unlabelled_migrations.append(rh.Finding("a/b", "#792", "shipped 1-*"))
        self.assertFalse(r.actionable)
        r.unrun_migrations.append(rh.Finding("a/b", "#849", "closed"))
        self.assertTrue(r.actionable, "an unrun migration is an incident")

    def test_unrun_migration_is_actionable(self):
        """hrse#867: a migration that closed without running is exactly the
        thing worth failing on — it blocked two issues for days on hrse#849."""
        r = rh.Report()
        r.unrun_migrations.append(rh.Finding("a/b", "#849", "closed 2026-08-13"))
        self.assertTrue(r.actionable)


class UnlabelledMigrationTests(unittest.TestCase):
    """hrse#871 — migration commits whose issue never got the label."""

    COMMIT = {"sha": "abc1234", "commit": {"message": "fix: backfill (#792)"}}

    def _sweep(self, files, issues, commit=None):
        """issues: one _rest response per distinct ref, in ascending order."""
        report = rh.Report()
        calls = iter([[commit or self.COMMIT], [{"files": files}]] + issues)
        with patch.object(rh, "_rest", side_effect=lambda *_: next(calls)):
            rh.audit_unlabelled_migrations("a/b", report)
        return report.unlabelled_migrations

    def test_migration_script_with_unlabelled_issue_is_flagged(self):
        found = self._sweep([{"filename": "scripts/1-backfill-x.py"}],
                            [[{"number": 792, "labels": []}]])
        self.assertEqual(len(found), 1)
        self.assertIn("scripts/1-backfill-x.py", found[0].detail)

    def test_labelled_issue_is_not_flagged(self):
        self.assertEqual(self._sweep(
            [{"filename": "scripts/1-backfill-x.py"}],
            [[{"number": 792, "labels": [{"name": "data-migration"}]}]]), [])

    def test_non_mutating_numbered_scripts_are_exempt(self):
        """Exempt by name because they are not issue-owned graph
        migrations -- not a claim that they write nothing."""
        for name in ("1-verify_x.py", "1-gate_x.py", "1-setup_x.py",
                     "1-diagnose_x.py"):
            with self.subTest(script=name):
                self.assertEqual(self._sweep(
                    [{"filename": f"scripts/{name}"}],
                    [[{"number": 792, "labels": []}]]), [])

    def test_h_shorthand_refs_are_matched(self):
        """H350/H511 refs hid 100% of that population -- silently."""
        commit = {"sha": "abc1234", "commit": {"message": "fix: dedupe (H350) (#655)"}}
        found = self._sweep([{"filename": "scripts/1-dedupe_x.py"}],
                            [[{"number": 350, "labels": []}],
                             [{"number": 655, "labels": [],
                               "pull_request": {"url": "x"}}]], commit=commit)
        self.assertEqual([f.name for f in found], ["#350"])

    def test_cross_repo_ref_is_not_looked_up_here(self):
        """forge#266 must not resolve to this repo's #266, which exists."""
        commit = {"sha": "abc1234",
                  "commit": {"message": "fix: x (harmonic-forge#266) (#840)"}}
        found = self._sweep([{"filename": "scripts/1-backfill-x.py"}],
                            [[{"number": 840, "labels": [],
                               "pull_request": {"url": "x"}}]], commit=commit)
        self.assertEqual(found, [])

    def test_non_script_commit_is_ignored(self):
        self.assertEqual(self._sweep([{"filename": "backend/app/main.py"}], []), [])

    def test_pull_request_ref_is_skipped(self):
        """The squash subject carries both the issue and the PR number;
        only the issue should be judged."""
        both = {"sha": "abc1234",
                "commit": {"message": "fix: backfill (#792) (#835)"}}
        found = self._sweep(
            [{"filename": "scripts/1-backfill-x.py"}],
            [[{"number": 792, "labels": []}],
             [{"number": 835, "labels": [], "pull_request": {"url": "x"}}]],
            commit=both)
        self.assertEqual([f.name for f in found], ["#792"])


class MigrationSweepTests(unittest.TestCase):
    """hrse#867 — closed data-migration issues with no execution record."""

    @staticmethod
    def _issue(number, labels, **extra):
        base = {"number": number, "title": "backfill something",
                "closed_at": "2026-08-13T23:30:21Z",
                "closed_by": {"login": "marc"},
                "labels": [{"name": n} for n in labels]}
        base.update(extra)
        return base

    def _sweep(self, issues):
        report = rh.Report()
        with patch.object(rh, "_rest", return_value=issues):
            rh.audit_migrations("vitalharmony/hrse", report)
        return report.unrun_migrations

    def test_unlabelled_execution_is_flagged(self):
        found = self._sweep([self._issue(849, ["data-migration", "tech-debt"])])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].name, "#849")
        self.assertIn("2026-08-13", found[0].detail)
        self.assertIn("marc", found[0].detail)

    def test_executed_label_clears_it(self):
        self.assertEqual(self._sweep([
            self._issue(849, ["data-migration", "migration-executed"])]), [])

    def test_abandoned_label_clears_it(self):
        self.assertEqual(self._sweep([
            self._issue(849, ["data-migration", "migration-abandoned"])]), [])

    def test_pull_requests_are_skipped(self):
        """The issues endpoint returns PRs too; a PR is not a migration."""
        pr = self._issue(853, ["data-migration"], pull_request={"url": "x"})
        self.assertEqual(self._sweep([pr]), [])

    def test_missing_closed_by_does_not_crash(self):
        found = self._sweep([self._issue(849, ["data-migration"], closed_by=None)])
        self.assertIn("unknown", found[0].detail)

    def test_no_label_parsing_of_comments(self):
        """The decision reads labels only -- prose is never a credential."""
        issue = self._issue(849, ["data-migration"],
                            body="MIGRATION-EXECUTED: rows=217")
        self.assertEqual(len(self._sweep([issue])), 1)


class PaginationTests(unittest.TestCase):
    def test_rest_concatenated_pages_are_all_parsed(self):
        """gh --paginate concatenates JSON arrays; naive json.loads sees only
        the first page and silently truncates — the hrse#800 failure class."""
        page1 = json.dumps([{"name": "a"}, {"name": "b"}])
        page2 = json.dumps([{"name": "c"}])
        with patch.object(rh, "_run", return_value=page1 + "\n" + page2):
            got = rh._rest("repos/x/y/branches")
        self.assertEqual([g["name"] for g in got], ["a", "b", "c"])


class AuditCheckoutBranchTests(unittest.TestCase):
    """hrse#808 — a shared main checkout drifting off `main` (hrse#277)."""

    def test_on_main_is_not_flagged(self):
        report = rh.Report()
        with patch.object(rh, "_run", return_value="main\n"):
            rh.audit_checkout_branch("/some/checkout", report)
        self.assertEqual(report.checkout_off_main, [])

    def test_off_main_is_flagged(self):
        report = rh.Report()
        with patch.object(rh, "_run", return_value="fix/error-handling\n"):
            rh.audit_checkout_branch("/some/checkout", report)
        self.assertEqual(len(report.checkout_off_main), 1)
        self.assertIn("fix/error-handling", report.checkout_off_main[0].name)

    def test_detached_head_is_flagged_not_crashed(self):
        """`git branch --show-current` prints nothing in detached HEAD."""
        report = rh.Report()
        with patch.object(rh, "_run", return_value="\n"):
            rh.audit_checkout_branch("/some/checkout", report)
        self.assertEqual(len(report.checkout_off_main), 1)
        self.assertIn("detached HEAD", report.checkout_off_main[0].detail)

    def test_git_failure_is_flagged_not_silently_clean(self):
        report = rh.Report()
        with patch.object(rh, "_run", side_effect=rh.GhError("not a repo")):
            rh.audit_checkout_branch("/some/checkout", report)
        self.assertEqual(len(report.checkout_off_main), 1)


class AuditStashesTests(unittest.TestCase):
    """hrse#808 — a stash is stranded work with less visibility than a branch."""

    def test_no_stashes_is_clean(self):
        report = rh.Report()
        with patch.object(rh, "_run", return_value=""):
            rh.audit_stashes("/some/checkout", report)
        self.assertEqual(report.stale_stashes, [])

    def test_every_stash_entry_is_reported(self):
        raw = (
            "stash@{0}: On fix/error-handling: priorities+settings updates in progress\n"
            "stash@{1}: On main: stray AGENTS.md change, investigating separately\n"
        )
        report = rh.Report()
        with patch.object(rh, "_run", return_value=raw):
            rh.audit_stashes("/some/checkout", report)
        self.assertEqual(len(report.stale_stashes), 2)
        self.assertEqual(report.stale_stashes[0].name, "stash@{0}")
        self.assertIn("priorities+settings", report.stale_stashes[0].detail)

    def test_never_calls_stash_drop_or_pop(self):
        """Report-only: assert the module never invokes a mutating stash verb."""
        calls: list[list[str]] = []

        def fake_run(args, cwd=None):
            calls.append(args)
            return "stash@{0}: On main: wip\n"

        report = rh.Report()
        with patch.object(rh, "_run", side_effect=fake_run):
            rh.audit_stashes("/some/checkout", report)
        for call in calls:
            self.assertNotIn("drop", call)
            self.assertNotIn("pop", call)
            self.assertNotIn("clear", call)


class AuditTransactionLogTests(unittest.TestCase):
    """hrse#808 — merged commits neither touching transaction-log.md nor
    backfilled into it.

    Two-signal design, corrected after a live run against hrse found the
    original single-signal (headline-match only) version producing 524/588
    false positives: a squash-merged PR's own final subject is the PR
    title, which was never any LOCAL commit's message, so it never matches
    any entry headline even when `mise run commit` genuinely ran somewhere
    in that PR's history. `recent` fixtures below use `git log --name-only
    --pretty=format:===%H===%s` shape: a `===sha===subject` marker line,
    then zero or more changed-file lines until the next marker.
    """

    def _run_audit(self, has_file, recent_commits, history_patch, fetch_ok=True):
        """recent_commits: list of (subject, touched_log: bool)."""
        report = rh.Report()
        calls = {"fetch": 0}

        recent_lines = []
        for i, (subject, touched) in enumerate(recent_commits):
            recent_lines.append(f"==={'a' * 7 + str(i)}==={subject}")
            if touched:
                recent_lines.append("transaction-log.md")
                recent_lines.append("some/other/file.py")
        recent_text = "\n".join(recent_lines) + ("\n" if recent_lines else "")

        def fake_run(args, cwd=None):
            if args[:2] == ["git", "fetch"]:
                calls["fetch"] += 1
                if not fetch_ok:
                    raise rh.GhError("no network")
                return ""
            if args[:3] == ["git", "log", "origin/main"]:
                return recent_text
            if args[:3] == ["git", "log", "-p"]:
                return history_patch
            raise AssertionError(f"unexpected call: {args}")

        with patch.object(rh, "_run", side_effect=fake_run), \
             patch.object(rh.Path, "is_file", return_value=has_file):
            rh.audit_transaction_log("/some/checkout", report)
        return report, calls

    def test_repo_without_the_file_is_silently_skipped(self):
        report, calls = self._run_audit(
            has_file=False, recent_commits=[("fix: x", False)], history_patch="")
        self.assertEqual(report.missing_transaction_log, [])
        self.assertEqual(calls["fetch"], 0, "must not even fetch for a repo with no log")

    def test_commit_that_never_touched_the_file_and_has_no_backfill_is_flagged(self):
        report, _ = self._run_audit(
            has_file=True,
            recent_commits=[("fix: real bug (#900)", False)],
            history_patch="+## build: Auto-bump to v2.7.0 and restart services\n")
        self.assertEqual(len(report.missing_transaction_log), 1)
        self.assertIn("fix: real bug (#900)", report.missing_transaction_log[0].name)

    def test_commit_that_touched_the_file_itself_is_not_flagged_even_without_a_headline_match(self):
        """The core fix: a multi-commit squash-merged PR's final subject
        never matches any single local commit's headline, but if its own
        diff touched transaction-log.md at all, `mise run commit` ran
        somewhere inside it -- that alone is enough, no headline match
        required."""
        report, _ = self._run_audit(
            has_file=True,
            recent_commits=[("Career 2.7-A: rank Discovery queue (#835)", True)],
            history_patch="+## build: Auto-bump to v2.6.83 and restart services\n")
        self.assertEqual(report.missing_transaction_log, [])

    def test_commit_with_a_matching_backfilled_headline_is_not_flagged(self):
        """hrse#817's real precedent: a LATER, separate commit backfilled
        entries headlined with each original merge's own subject. Neither
        commit necessarily touched the file in the same diff as the other,
        so the headline-match path stays load-bearing for this case."""
        report, _ = self._run_audit(
            has_file=True,
            recent_commits=[("fix: real bug (#900)", False)],
            history_patch="+## fix: real bug (#900)\n+- some/file.py | 1 +\n")
        self.assertEqual(report.missing_transaction_log, [])

    def test_historical_entry_predating_a_clear_still_counts(self):
        """The file is cleared periodically (version bump for hrse, push to
        main for harmonic-forge) -- an entry added long ago and since
        cleared from the LIVE file must still be found via the full
        `git log -p` walk, not a grep of current content."""
        history = (
            "+## fix: old work (#700)\n"          # added by an earlier commit
            "+- some/file.py | 1 +\n"
            "-## fix: old work (#700)\n"           # later cleared
            "-- some/file.py | 1 +\n"
            "+## build: Auto-bump to v2.8.0 and restart services\n"
        )
        report, _ = self._run_audit(
            has_file=True, recent_commits=[("fix: old work (#700)", False)],
            history_patch=history)
        self.assertEqual(report.missing_transaction_log, [])

    def test_duplicate_undocumented_subjects_flagged_once(self):
        report, _ = self._run_audit(
            has_file=True,
            recent_commits=[("fix: real bug (#900)", False), ("fix: real bug (#900)", False)],
            history_patch="")
        self.assertEqual(len(report.missing_transaction_log), 1)

    def test_fetch_failure_does_not_abort_the_check(self):
        """Best-effort freshness -- a failed `git fetch` still lets the check
        run against whatever local origin/main state already exists."""
        report, calls = self._run_audit(
            has_file=True, recent_commits=[("fix: real bug (#900)", False)],
            history_patch="", fetch_ok=False)
        self.assertEqual(calls["fetch"], 1)
        self.assertEqual(len(report.missing_transaction_log), 1)

    def test_last_commit_in_the_window_is_still_flushed(self):
        """The trailing commit has no following marker line to trigger its
        own flush -- the final _flush() call after the loop must catch it."""
        report, _ = self._run_audit(
            has_file=True,
            recent_commits=[("fix: a (#1)", True), ("fix: b (#2)", False)],
            history_patch="")
        self.assertEqual(len(report.missing_transaction_log), 1)
        self.assertIn("fix: b (#2)", report.missing_transaction_log[0].name)


class BoardOwnerTypeTests(unittest.TestCase):
    """hrse#808/#991 — `projectV2` owner is not polymorphic; must try both."""

    def test_organization_owner_resolves_on_first_try(self):
        payload = json.dumps({"data": {"organization": {"projectV2": {
            "items": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                       "nodes": []}}}}})
        with patch.object(rh, "_run", return_value=payload) as mock_run:
            state = rh._board_state("some-org", "3")
        self.assertEqual(state, {})
        # First (and only) call must be the organization-typed query.
        first_call_args = mock_run.call_args_list[0][0][0]
        self.assertIn(f"query={rh._BOARD_ITEMS_QUERY_ORG}", first_call_args)

    def test_user_owner_falls_back_after_organization_returns_a_graceful_null(self):
        org_miss = json.dumps({"data": {"organization": None}})
        user_hit = json.dumps({"data": {"user": {"projectV2": {
            "items": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                       "nodes": []}}}}})
        with patch.object(rh, "_run", side_effect=[org_miss, user_hit]) as mock_run:
            state = rh._board_state("vitalharmony", "1")
        self.assertEqual(state, {})
        self.assertEqual(mock_run.call_count, 2)

    def test_user_owner_falls_back_after_organization_raises_gherror(self):
        """Confirmed live (hrse#808): `gh api graphql` exits NONZERO for
        `organization(login: $owner)` against a real user-owned login --
        "Could not resolve to an Organization with the login of
        'vitalharmony'" -- rather than a graceful `data.organization: null`.
        This is the actual failure mode in production; the graceful-null
        test above covers a schema-level possibility, this one covers what
        `gh` itself really does."""
        user_hit = json.dumps({"data": {"user": {"projectV2": {
            "items": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                       "nodes": []}}}}})

        def fake_run(args, cwd=None):
            if f"query={rh._BOARD_ITEMS_QUERY_ORG}" in args:
                raise rh.GhError("Could not resolve to an Organization with the login of 'vitalharmony'.")
            return user_hit

        with patch.object(rh, "_run", side_effect=fake_run) as mock_run:
            state = rh._board_state("vitalharmony", "1")
        self.assertEqual(state, {})
        self.assertEqual(mock_run.call_count, 2)

    def test_neither_owner_type_resolves_raises(self):
        both_miss = json.dumps({"data": {"organization": None, "user": None}})
        with patch.object(rh, "_run", return_value=both_miss):
            with self.assertRaises(rh.GhError):
                rh._board_state("ghost", "1")

    def test_organization_gherror_then_user_also_missing_still_raises(self):
        user_miss = json.dumps({"data": {"user": None}})

        def fake_run(args, cwd=None):
            if f"query={rh._BOARD_ITEMS_QUERY_ORG}" in args:
                raise rh.GhError("Could not resolve to an Organization with the login of 'ghost'.")
            return user_miss

        with patch.object(rh, "_run", side_effect=fake_run):
            with self.assertRaises(rh.GhError):
                rh._board_state("ghost", "1")

    def test_graphql_errors_propagate_as_gherror(self):
        errored = json.dumps({"errors": [{"message": "rate limited"}]})
        with patch.object(rh, "_run", return_value=errored):
            with self.assertRaises(rh.GhError):
                rh._board_state("vitalharmony", "1")


class NewChecksAreReportOnlyTests(unittest.TestCase):
    """hrse#808's own Load-Bearing Assumptions: report-only, no new failing
    condition, unless explicitly disclosed as a deviation (none here)."""

    def test_checkout_off_main_alone_does_not_fail(self):
        r = rh.Report()
        r.checkout_off_main.append(rh.Finding("c", "feat/x", "expected 'main'"))
        self.assertFalse(r.actionable)

    def test_stale_stashes_alone_does_not_fail(self):
        r = rh.Report()
        r.stale_stashes.append(rh.Finding("c", "stash@{0}", "wip"))
        self.assertFalse(r.actionable)

    def test_missing_transaction_log_alone_does_not_fail(self):
        r = rh.Report()
        r.missing_transaction_log.append(rh.Finding("c", "fix: x", "no entry"))
        self.assertFalse(r.actionable)

    def test_stranded_still_fails_alongside_all_three_new_categories(self):
        r = rh.Report()
        r.checkout_off_main.append(rh.Finding("c", "feat/x", "expected 'main'"))
        r.stale_stashes.append(rh.Finding("c", "stash@{0}", "wip"))
        r.missing_transaction_log.append(rh.Finding("c", "fix: x", "no entry"))
        r.stranded.append(rh.Finding("c", "b", "real work"))
        self.assertTrue(r.actionable)


def _board_node(number, *, issue_state, status, repo="vitalharmony/hrse"):
    return {
        "content": {"number": number, "state": issue_state,
                     "repository": {"nameWithOwner": repo}},
        "theme": {"name": "Tooling"},
        "venture": {"name": "Platform"},
        "status": ({"name": status} if status is not None else None),
    }


class BoardStatusDriftTests(unittest.TestCase):
    """harmonic-forge#430: a board card left un-advanced after its issue
    closes. Report-only, never flips Status."""

    def test_closed_issue_not_marked_done_is_flagged(self):
        payload = json.dumps({"data": {"organization": {"projectV2": {
            "items": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                       "nodes": [_board_node(42, issue_state="CLOSED", status="In Progress")]}}}}})
        report = rh.Report()
        with patch.object(rh, "_run", return_value=payload):
            rh.audit_board_status_drift("vitalharmony/hrse", report, {})
        self.assertEqual(len(report.board_status_drift), 1)
        self.assertIn("#42", report.board_status_drift[0].name)
        self.assertIn("In Progress", report.board_status_drift[0].detail)

    def test_closed_issue_marked_done_is_not_flagged(self):
        payload = json.dumps({"data": {"organization": {"projectV2": {
            "items": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                       "nodes": [_board_node(42, issue_state="CLOSED", status="Done")]}}}}})
        report = rh.Report()
        with patch.object(rh, "_run", return_value=payload):
            rh.audit_board_status_drift("vitalharmony/hrse", report, {})
        self.assertEqual(report.board_status_drift, [])

    def test_open_issue_is_never_flagged_regardless_of_status(self):
        payload = json.dumps({"data": {"organization": {"projectV2": {
            "items": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                       "nodes": [_board_node(42, issue_state="OPEN", status="Todo")]}}}}})
        report = rh.Report()
        with patch.object(rh, "_run", return_value=payload):
            rh.audit_board_status_drift("vitalharmony/hrse", report, {})
        self.assertEqual(report.board_status_drift, [])

    def test_closed_issue_with_unset_status_is_flagged(self):
        """No Status at all is not silently read as Done."""
        payload = json.dumps({"data": {"organization": {"projectV2": {
            "items": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                       "nodes": [_board_node(42, issue_state="CLOSED", status=None)]}}}}})
        report = rh.Report()
        with patch.object(rh, "_run", return_value=payload):
            rh.audit_board_status_drift("vitalharmony/hrse", report, {})
        self.assertEqual(len(report.board_status_drift), 1)

    def test_never_writes_to_the_board(self):
        """Report-only: no mutating gh api call is ever issued."""
        payload = json.dumps({"data": {"organization": {"projectV2": {
            "items": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                       "nodes": [_board_node(42, issue_state="CLOSED", status="In Progress")]}}}}})
        report = rh.Report()
        with patch.object(rh, "_run", return_value=payload) as mock_run:
            rh.audit_board_status_drift("vitalharmony/hrse", report, {})
        for call in mock_run.call_args_list:
            args = call[0][0]
            self.assertNotIn("-X", args)
            self.assertNotIn("PATCH", args)
            self.assertNotIn("POST", args)

    def test_repo_not_on_any_board_is_silently_skipped(self):
        """`audit_unboarded` already reports the missing mapping once —
        this check must not report it a second time under a different name."""
        report = rh.Report()
        with patch.object(rh, "_run") as mock_run:
            rh.audit_board_status_drift("vitalharmony/no-such-repo", report, {})
        mock_run.assert_not_called()
        self.assertEqual(report.board_status_drift, [])

    def test_pagination_is_not_silently_truncated_past_30_items(self):
        """harmonic-forge#430's own named trap: the investigation that filed
        this issue produced a false '49 stale rows' finding from a `gh
        issue list` call that silently truncated at its 30-result default.
        This check must page fully via `hasNextPage`/`endCursor`, not stop
        at one page — exercised here against 45 open-board items (>30) plus
        one closed-and-drifted item split across two pages."""
        page1_nodes = [_board_node(n, issue_state="OPEN", status="Todo")
                       for n in range(1, 46)]
        page1 = json.dumps({"data": {"organization": {"projectV2": {
            "items": {"pageInfo": {"hasNextPage": True, "endCursor": "CURSOR1"},
                       "nodes": page1_nodes}}}}})
        page2 = json.dumps({"data": {"organization": {"projectV2": {
            "items": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                       "nodes": [_board_node(999, issue_state="CLOSED", status="In Review")]}}}}})
        report = rh.Report()
        with patch.object(rh, "_run", side_effect=[page1, page2]) as mock_run:
            rh.audit_board_status_drift("vitalharmony/hrse", report, {})
        self.assertEqual(mock_run.call_count, 2)
        self.assertEqual(len(report.board_status_drift), 1)
        self.assertIn("#999", report.board_status_drift[0].name)

    def test_board_status_drift_alone_does_not_fail_the_run(self):
        report = rh.Report()
        report.board_status_drift.append(rh.Finding("r", "#1", "not Done"))
        self.assertFalse(report.actionable)

    def test_shares_the_cache_with_audit_unboarded_for_the_same_board(self):
        """One fetch per board serves both checks when they share a cache —
        the two checks must not double the GraphQL cost for every repo."""
        payload = json.dumps({"data": {"organization": {"projectV2": {
            "items": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                       "nodes": [_board_node(42, issue_state="CLOSED", status="Done")]}}}}})
        report = rh.Report()
        cache: dict = {}
        with patch.object(rh, "_run", return_value=payload) as mock_run, \
                patch.object(rh, "_rest", return_value=[]):
            rh.audit_unboarded("vitalharmony/hrse", report, cache)
            rh.audit_board_status_drift("vitalharmony/hrse", report, cache)
        self.assertEqual(mock_run.call_count, 1)


class MainCleanGuardTests(unittest.TestCase):
    """A run with only new-category findings must not also print 'clean' —
    the hrse#808 handoff's explicit landmine: `main()`'s clean-guard
    enumerates every Report category by hand and is easy to leave stale."""

    def _run_main_with_one_checkout_finding(self, category):
        import io
        from contextlib import redirect_stdout

        def fake_audit_transaction_log(checkout, report):
            getattr(report, category).append(rh.Finding(checkout, "x", "y"))

        def noop(*_args, **_kwargs):
            return None

        buf = io.StringIO()
        with patch.object(sys, "argv", ["repo_hygiene.py", "--checkout", "/c"]), \
             patch.object(rh, "audit_worktrees", noop), \
             patch.object(rh, "audit_checkout_branch", noop), \
             patch.object(rh, "audit_stashes", noop), \
             patch.object(rh, "audit_transaction_log", fake_audit_transaction_log), \
             redirect_stdout(buf):
            rc = rh.main()
        return rc, buf.getvalue()

    def test_missing_transaction_log_alone_does_not_print_clean(self):
        rc, out = self._run_main_with_one_checkout_finding("missing_transaction_log")
        self.assertNotIn("repo hygiene: clean.", out)
        self.assertIn("MISSING TRANSACTION-LOG ENTRIES", out)
        self.assertEqual(rc, 0, "report-only category must not fail the run")

    def test_truly_clean_run_still_prints_clean(self):
        import io
        from contextlib import redirect_stdout

        def noop(*_args, **_kwargs):
            return None

        buf = io.StringIO()
        with patch.object(sys, "argv", ["repo_hygiene.py", "--checkout", "/c"]), \
             patch.object(rh, "audit_worktrees", noop), \
             patch.object(rh, "audit_checkout_branch", noop), \
             patch.object(rh, "audit_stashes", noop), \
             patch.object(rh, "audit_transaction_log", noop), \
             redirect_stdout(buf):
            rc = rh.main()
        self.assertIn("repo hygiene: clean.", buf.getvalue())
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()


class AuditUnboardedTests(unittest.TestCase):
    """hrse#979 — open issues invisible to board-driven reporting."""

    def _state(self, entries):
        return dict(entries)

    def _issues(self, *specs):
        return [{"number": n, "title": t, **({"pull_request": {}} if pr else {})}
                for n, t, pr in specs]

    def _run_audit(self, repo, issues, board_state):
        report = rh.Report()
        with patch.object(rh, "_rest", return_value=issues), \
             patch.object(rh, "_board_state", return_value=board_state):
            rh.audit_unboarded(repo, report, _cache={})
        return report

    def test_unboarded_issue_is_reported(self):
        r = self._run_audit("vitalharmony/hrse",
                            self._issues((42, "off board", False)), {})
        self.assertEqual(len(r.unboarded), 1)
        self.assertIn("on no board", r.unboarded[0].detail)

    def test_boarded_with_both_fields_is_clean(self):
        r = self._run_audit(
            "vitalharmony/hrse", self._issues((42, "fine", False)),
            {("vitalharmony/hrse", 42): {"Theme": "Tooling", "Venture": "CymaGraph"}})
        self.assertEqual(r.unboarded, [])

    def test_boarded_but_unthemed_is_reported(self):
        """The reopen case: on the board, fields never set."""
        r = self._run_audit(
            "vitalharmony/hrse", self._issues((917, "reopened", False)),
            {("vitalharmony/hrse", 917): {"Theme": None, "Venture": None}})
        self.assertEqual(len(r.unboarded), 1)
        self.assertIn("Theme and Venture unset", r.unboarded[0].detail)

    def test_pull_requests_are_skipped(self):
        r = self._run_audit("vitalharmony/hrse",
                            self._issues((99, "a PR", True)), {})
        self.assertEqual(r.unboarded, [])

    def test_unmapped_repo_is_reported_not_crashed(self):
        report = rh.Report()
        rh.audit_unboarded("vitalharmony/unknown", report, _cache={})
        self.assertEqual(len(report.unboarded), 1)
        self.assertIn("no board mapped", report.unboarded[0].detail)

    def test_milestones_are_not_checked(self):
        """Scope item 4: harmonic-forge carries no release milestones by
        decision, so milestone coverage must never be reported here."""
        self.assertNotIn("Milestone", rh._REQUIRED_BOARD_FIELDS)
        self.assertNotIn("milestone", rh._BOARD_ITEMS_QUERY.lower())

    def test_unboarded_alone_does_not_fail_the_run(self):
        """Scope item 3: exit code must not change for this category."""
        report = rh.Report()
        report.unboarded.append(rh.Finding("r", "#1", "on no board"))
        self.assertFalse(report.actionable)

    def test_stranded_still_fails(self):
        report = rh.Report()
        report.unboarded.append(rh.Finding("r", "#1", "on no board"))
        report.stranded.append(rh.Finding("r", "b", "real work"))
        self.assertTrue(report.actionable)


class WorktreePruneTests(unittest.TestCase):
    """hrse#427 AC2-6. Every git/gh call is mocked -- no test may depend on
    the operator's real /tmp state (AC7). Worktree paths are real temp
    directories: `prune_worktrees` stats each path with `Path.is_dir()`
    before evaluating it (a worktree can be registered in git's metadata
    with its directory already gone from disk -- live-caught, see the
    "missing from disk" test below), so a path that doesn't exist on disk
    is a distinct case under test, not an artifact to route around."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.checkout = str(root / "checkout")
        self.merged = str(root / "w-merged")
        self.dirty = str(root / "w-dirty")
        self.stranded = str(root / "w-stranded")
        self.lane = str(root / "HRSE2-lane2")
        self.gone = str(root / "w-gone")  # never created -- the missing-from-disk case
        for p in (self.checkout, self.merged, self.dirty, self.stranded, self.lane):
            Path(p).mkdir()
        self.porcelain = (
            f"worktree {self.checkout}\n"
            "HEAD aaaa\n"
            "branch refs/heads/main\n"
            "\n"
            f"worktree {self.merged}\n"
            "HEAD bbbb\n"
            "branch refs/heads/feat/merged\n"
            "\n"
            f"worktree {self.dirty}\n"
            "HEAD cccc\n"
            "branch refs/heads/feat/dirty\n"
            "\n"
            f"worktree {self.stranded}\n"
            "HEAD dddd\n"
            "branch refs/heads/feat/stranded\n"
            "\n"
            f"worktree {self.lane}\n"
            "HEAD eeee\n"
            "branch refs/heads/main\n"
            "\n"
            f"worktree {self.gone}\n"
            "HEAD ffff\n"
            "branch refs/heads/feat/gone\n"
            "\n"
        )

    def _fake_run(self, args, cwd=None):
        if args[:3] == ["git", "remote", "get-url"]:
            return "git@github.com:acme/repo.git\n"
        if args[:3] == ["git", "worktree", "list"]:
            return self.porcelain
        if args[:2] == ["gh", "pr"]:
            branch = args[args.index("--head") + 1]
            return json.dumps([{"number": 42}]) if branch == "feat/merged" else json.dumps([])
        if args[:2] == ["git", "status"]:
            return " M some_file.py\n" if cwd == self.dirty else ""
        if args[:2] == ["git", "cherry"]:
            return "+ abc123 unmerged commit\n" if cwd == self.stranded else "- abc123 already upstream\n"
        if args[:3] in (["git", "worktree", "remove"], ["git", "worktree", "prune"]):
            return ""
        raise AssertionError(f"unexpected call: {' '.join(args)} (cwd={cwd})")

    def test_repo_for_checkout_parses_ssh_remote(self):
        with patch.object(rh, "_run", side_effect=self._fake_run):
            self.assertEqual(rh._repo_for_checkout(self.checkout), "acme/repo")

    def test_squash_merged_branch_is_prunable(self):
        """Trap 1: git cherry (patch-id) says this branch is fully
        represented upstream even though it has no ancestry link to main --
        the shape every squash-merged branch in this project has."""
        with patch.object(rh, "_run", side_effect=self._fake_run):
            c = rh.evaluate_worktree_prunability("acme/repo", self.merged, "feat/merged")
        self.assertTrue(c.prunable)
        self.assertIn("PR #42 merged", c.reasons[0])

    def test_uncommitted_changes_block_pruning(self):
        with patch.object(rh, "_run", side_effect=self._fake_run):
            c = rh.evaluate_worktree_prunability("acme/repo", self.dirty, "feat/dirty")
        self.assertFalse(c.prunable)
        self.assertTrue(any("uncommitted" in r for r in c.reasons))

    def test_no_merged_pr_and_unmatched_commits_is_stranded(self):
        """Trap 2: absent from origin with no merged PR must never be
        treated as 'safe to prune' -- reported as stranded instead, with
        both failing conditions named."""
        with patch.object(rh, "_run", side_effect=self._fake_run):
            c = rh.evaluate_worktree_prunability("acme/repo", self.stranded, "feat/stranded")
        self.assertFalse(c.prunable)
        self.assertTrue(any("no merged PR" in r for r in c.reasons))
        self.assertTrue(any("no patch-equivalent" in r for r in c.reasons))

    def test_protected_lane_worktree_is_never_evaluated(self):
        calls = []

        def recording(args, cwd=None):
            calls.append(cwd)
            return self._fake_run(args, cwd)

        with patch.object(rh, "_run", side_effect=recording):
            rh.prune_worktrees(self.checkout, dry_run=True)
        self.assertNotIn(self.lane, calls)

    def test_missing_from_disk_is_never_evaluated_and_never_crashes(self):
        """Live-caught: a worktree registered in git's metadata with its
        directory already removed by hand crashed `git status`/`git
        cherry` with FileNotFoundError (not GhError) before this guard."""
        calls = []

        def recording(args, cwd=None):
            calls.append(cwd)
            return self._fake_run(args, cwd)

        with patch.object(rh, "_run", side_effect=recording):
            code = rh.prune_worktrees(self.checkout, dry_run=True)
        self.assertEqual(code, 0)
        self.assertNotIn(self.gone, calls)

    def test_dry_run_removes_nothing(self):
        removed = []

        def recording(args, cwd=None):
            if args[:3] == ["git", "worktree", "remove"]:
                removed.append(args[3])
            return self._fake_run(args, cwd)

        with patch.object(rh, "_run", side_effect=recording):
            code = rh.prune_worktrees(self.checkout, dry_run=True)
        self.assertEqual(code, 0)
        self.assertEqual(removed, [])

    def test_execute_removes_only_the_prunable_worktree(self):
        removed = []

        def recording(args, cwd=None):
            if args[:3] == ["git", "worktree", "remove"]:
                removed.append(args[3])
                return ""
            return self._fake_run(args, cwd)

        with patch.object(rh, "_run", side_effect=recording):
            code = rh.prune_worktrees(self.checkout, dry_run=False)
        self.assertEqual(code, 0)
        self.assertEqual(removed, [self.merged])

    def test_execute_still_prunes_metadata_when_only_missing_worktrees_exist(self):
        """A registered-but-missing worktree has no data-loss risk -- `git
        worktree prune` must still run to clear its metadata even when
        nothing is actually removable via `git worktree remove`."""
        pruned = []

        def recording(args, cwd=None):
            if args[:3] == ["git", "worktree", "prune"]:
                pruned.append(cwd)
                return ""
            return self._fake_run(args, cwd)

        porcelain_missing_only = (
            f"worktree {self.checkout}\n"
            "HEAD aaaa\n"
            "branch refs/heads/main\n"
            "\n"
            f"worktree {self.gone}\n"
            "HEAD ffff\n"
            "branch refs/heads/feat/gone\n"
            "\n"
        )
        with patch.object(rh, "_run", side_effect=recording), \
                patch.object(self, "porcelain", porcelain_missing_only):
            code = rh.prune_worktrees(self.checkout, dry_run=False)
        self.assertEqual(code, 0)
        self.assertEqual(pruned, [self.checkout])

    def test_main_checkout_itself_is_never_a_candidate(self):
        with patch.object(rh, "_run", side_effect=self._fake_run):
            entries = rh._worktree_entries(self.checkout)
        self.assertNotIn(self.checkout, [p for p, _ in entries])

