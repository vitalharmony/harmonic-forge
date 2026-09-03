#!/usr/bin/env python3
"""Unit tests for watch_lane_posts.py (harmonic-forge#442) -- pure parsing
logic only, no live gh/API calls. Fixtures are real comment bodies from
hrse#1530 (trimmed), not invented shapes."""
import subprocess
import tempfile
import unittest
from pathlib import Path

from watch_lane_posts import _BRANCH_ISSUE_RE, _classify, discover_from_worktree


class ClassifyTests(unittest.TestCase):
    def test_l1_post_marker_wins_regardless_of_heading(self):
        body = ("## Handoff: hrse#1530 — some title\n\nbody text\n\n"
                "<!-- l1-post v1; kind=handoff; posted-by=LANE-unset -->")
        self.assertEqual(_classify(body), ("l1", "handoff"))

    def test_l1_ready_for_l3_marker_with_extra_fields(self):
        body = ("## Ready for Lane 3 — hrse#1530\n\nbody\n\n"
                "<!-- l1-post v1; kind=ready-for-l3; sha=abc123; checks=x,y -->")
        self.assertEqual(_classify(body), ("l1", "ready-for-l3"))

    def test_l2_heading_no_marker(self):
        body = "## L2D — receipt-backed status (harmonic-forge#371)\n\nsome narrative"
        self.assertEqual(_classify(body), ("l2", "## L2D — receipt-backed status (harmonic-forge#371)"))

    def test_l2_plan_heading(self):
        body = "## L2P — receipt-backed status (harmonic-forge#371)\n\nplan text"
        lane, _ = _classify(body)
        self.assertEqual(lane, "l2")

    def test_l3_heading_spelled_out_not_a_short_code(self):
        body = "## Lane 3 Test Spec — hrse#1530 (NULL-tolerant sync predicate)\n\nspec text"
        lane, _ = _classify(body)
        self.assertEqual(lane, "l3")

    def test_plain_comment_is_unclassified(self):
        self.assertIsNone(_classify("just a plain chat comment, no heading, no marker"))

    def test_unrelated_heading_is_unclassified(self):
        self.assertIsNone(_classify("## Some other heading entirely\n\ntext"))


class BranchIssueRegexTests(unittest.TestCase):
    """Real branch names observed live across this repo's worktrees,
    2026-09-03 -- not invented shapes."""

    def _num_prefix(self, branch):
        m = _BRANCH_ISSUE_RE.search(branch)
        if not m:
            return None
        return m.group("prefix"), m.group("num")

    def test_l2_h_prefixed(self):
        self.assertEqual(self._num_prefix("l2/h1530-null-tolerant-sync-predicate"),
                         ("h", "1530"))

    def test_h_prefixed_no_lane_segment(self):
        self.assertEqual(self._num_prefix("h1522/tier-group-rename"), ("h", "1522"))

    def test_bare_digits_no_letter_prefix(self):
        self.assertEqual(self._num_prefix("fix/1498-workflow-secrets-context"),
                         (None, "1498"))

    def test_f_prefixed_cross_repo_subject(self):
        self.assertEqual(self._num_prefix("l2/f433-drift-check-patch-id"), ("f", "433"))

    def test_bare_digits_spike_branch(self):
        self.assertEqual(self._num_prefix("spike/733-plan"), (None, "733"))

    def test_no_issue_number_returns_none(self):
        self.assertIsNone(self._num_prefix("docs/priorities-reconcile-sep3"))
        self.assertIsNone(self._num_prefix("docs/transaction-log-regen-sep3"))


class DiscoverFromWorktreeTests(unittest.TestCase):
    """End-to-end against real temporary git repos -- no network, no gh."""

    def _repo(self, remote_url: str, branch: str) -> str:
        tmp = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", "-b", branch, tmp], check=True)
        subprocess.run(["git", "-C", tmp, "remote", "add", "origin", remote_url], check=True)
        subprocess.run(["git", "-C", tmp, "commit", "-q", "--allow-empty", "-m", "x"],
                       check=True, env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                                        "PATH": __import__("os").environ.get("PATH", "")})
        return tmp

    def test_h_prefix_overrides_worktree_repo(self):
        """A hrse-repo worktree hosting a branch about a forge issue must
        resolve to harmonic-forge -- the real F433 case."""
        repo = self._repo("git@github.com:vitalharmony/hrse.git",
                          "l2/f433-drift-check-patch-id")
        self.assertEqual(discover_from_worktree(repo),
                         ("vitalharmony/harmonic-forge", 433))

    def test_unprefixed_number_uses_worktree_own_repo(self):
        repo = self._repo("https://github.com/vitalharmony/hrse.git",
                          "fix/1498-workflow-secrets-context")
        self.assertEqual(discover_from_worktree(repo), ("vitalharmony/hrse", 1498))

    def test_branch_with_no_issue_number_is_none(self):
        repo = self._repo("https://github.com/vitalharmony/hrse.git",
                          "docs/priorities-reconcile-sep3")
        self.assertIsNone(discover_from_worktree(repo))

    def test_non_git_path_is_none(self):
        tmp = tempfile.mkdtemp()
        self.assertIsNone(discover_from_worktree(tmp))


if __name__ == "__main__":
    unittest.main()
