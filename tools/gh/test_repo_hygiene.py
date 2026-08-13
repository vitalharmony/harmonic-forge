#!/usr/bin/env python3
"""Tests for repo_hygiene.py (hrse#808)."""

import json
import unittest
from unittest.mock import patch

import repo_hygiene as rh


class ClassificationTests(unittest.TestCase):
    """Branch classification. Every case here is one the 2026-08-12 manual
    sweep actually produced."""

    def _audit(self, branches, prs, compares, default="main"):
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
                return json.dumps({"ahead": compares.get(branch, 0)})
            raise AssertionError(f"unexpected call: {joined}")

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
        """The case that matters — work that exists nowhere else."""
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
        self.assertEqual(len(report.stranded), 1)
        self.assertEqual(report.orphaned, [])


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


class PaginationTests(unittest.TestCase):
    def test_rest_concatenated_pages_are_all_parsed(self):
        """gh --paginate concatenates JSON arrays; naive json.loads sees only
        the first page and silently truncates — the hrse#800 failure class."""
        page1 = json.dumps([{"name": "a"}, {"name": "b"}])
        page2 = json.dumps([{"name": "c"}])
        with patch.object(rh, "_run", return_value=page1 + "\n" + page2):
            got = rh._rest("repos/x/y/branches")
        self.assertEqual([g["name"] for g in got], ["a", "b", "c"])


if __name__ == "__main__":
    unittest.main()
