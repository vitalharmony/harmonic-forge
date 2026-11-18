#!/usr/bin/env python3
"""harmonic-forge#1393 round 2 (sticky-wicket PATCH): the branch-ahead notice against REAL git
history. The first version counted commits by author date against the completion's time, and its
mocked-`_run_git` tests carried the same date assumption, which is how an amend after an L2D
slipped through. These tests build real temporary repos and run real `git commit --amend`,
`git rebase --autosquash` and `git rebase` onto an advanced main.

Run: python3 tools/gh/test_branch_ahead_real_git.py
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import l2_post as lp  # noqa: E402
import watch_lane_posts as W  # noqa: E402

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_EDITOR": "true",
       "GIT_SEQUENCE_EDITOR": "true"}


def git(cwd, *args, check=True):
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=ENV)
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {result.stderr}")
    return result.stdout.strip()


class Repo(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.origin = root / "origin"
        self.wt = root / "wt"
        self.origin.mkdir()
        git(self.origin, "init", "-q", "-b", "main")
        (self.origin / "a.txt").write_text("a\n")
        git(self.origin, "add", "-A")
        git(self.origin, "commit", "-q", "-m", "base")
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.wt)], check=True, env=ENV)
        git(self.wt, "checkout", "-q", "-b", "fix/1-x")
        self.n = 0

    def commit(self, text, name="b.txt", message=None):
        (self.wt / name).write_text(text)
        git(self.wt, "add", "-A")
        self.n += 1
        git(self.wt, "commit", "-q", "-m", message or f"work {self.n}")
        return git(self.wt, "rev-parse", "HEAD")

    def report(self, completion_sha, comments_extra=()):
        body = lp.compose_body("completion", [], "clean", lead={
            "Status": "done", "Change": f"x at commit {completion_sha}", "Next": "Lane 1 to review"})
        comments = [{"body": body, "created_at": "2020-01-01T00:00:00Z"}, *comments_extra]
        with mock.patch.object(W, "_fetch_all_comments", return_value=comments), \
                mock.patch.object(W, "_issue_meta", return_value=("open", set(), None)):
            return W.branch_ahead_without_completion(str(self.wt), "o/r", 1)

    def advance_main(self):
        (self.origin / "m.txt").write_text("m\n")
        git(self.origin, "add", "-A")
        git(self.origin, "commit", "-q", "-m", "main moves")
        git(self.wt, "fetch", "-q", "origin")


class BranchAheadRealGit(Repo):
    def test_reported_work_with_nothing_after_it_is_the_steady_state(self):
        sha = self.commit("one\n")
        self.assertIsNone(self.report(sha))

    def test_an_amend_after_the_completion_announces(self):
        sha = self.commit("one\n")
        (self.wt / "b.txt").write_text("one reworked\n")
        git(self.wt, "add", "-A")
        git(self.wt, "commit", "-q", "--amend", "--no-edit")
        report = self.report(sha)
        self.assertIsNotNone(report)
        self.assertIn("since its last completion", report)

    def test_a_fixup_plus_autosquash_after_the_completion_announces(self):
        sha = self.commit("one\n")
        (self.wt / "b.txt").write_text("one reworked\n")
        git(self.wt, "add", "-A")
        git(self.wt, "commit", "-q", "--fixup", sha)
        git(self.wt, "rebase", "-q", "-i", "--autosquash", "origin/main")
        self.assertIsNotNone(self.report(sha))

    def test_a_rebase_onto_an_advanced_main_with_no_content_change_does_not_announce(self):
        sha = self.commit("one\n")
        self.advance_main()
        git(self.wt, "rebase", "-q", "origin/main")
        self.assertNotEqual(git(self.wt, "rev-parse", "HEAD"), sha)
        self.assertIsNone(self.report(sha))

    def test_an_amend_that_changes_only_the_message_does_not_announce(self):
        sha = self.commit("one\n")
        git(self.wt, "commit", "-q", "--amend", "-m", "reworded")
        self.assertIsNone(self.report(sha))

    def test_a_new_commit_after_the_completion_announces_with_its_count(self):
        sha = self.commit("one\n")
        self.commit("two\n", name="c.txt")
        report = self.report(sha)
        self.assertIsNotNone(report)
        self.assertIn("1 commit ahead", report)

    def test_an_unparseable_reported_sha_falls_back_to_dates_and_never_raises(self):
        self.commit("one\n")
        body = lp.compose_body("completion", [], "clean", lead={
            "Status": "done", "Change": "x", "Next": "review"})
        with mock.patch.object(W, "_fetch_all_comments",
                               return_value=[{"body": body, "created_at": "2099-01-01T00:00:00Z"}]), \
                mock.patch.object(W, "_issue_meta", return_value=("open", set(), None)):
            self.assertIsNone(W.branch_ahead_without_completion(str(self.wt), "o/r", 1))

    def test_a_reported_sha_missing_locally_returns_none(self):
        self.commit("one\n")
        self.assertIsNone(self.report("deadbeefcafe"))

    def test_a_git_failure_in_the_patch_id_returns_none(self):
        sha = self.commit("one\n")
        with mock.patch.object(W, "_patch_id", return_value=None):
            self.assertIsNone(self.report(sha))

    def test_the_tooling_exception_label_still_suppresses_the_notice(self):
        sha = self.commit("one\n")
        (self.wt / "b.txt").write_text("reworked\n")
        git(self.wt, "add", "-A")
        git(self.wt, "commit", "-q", "--amend", "--no-edit")
        body = lp.compose_body("completion", [], "clean", lead={
            "Status": "done", "Change": f"commit {sha}", "Next": "review"})
        with mock.patch.object(W, "_fetch_all_comments",
                               return_value=[{"body": body, "created_at": "2020-01-01T00:00:00Z"}]), \
                mock.patch.object(W, "_issue_meta",
                                  return_value=("open", {"tooling-exception"}, None)):
            self.assertIsNone(W.branch_ahead_without_completion(str(self.wt), "o/r", 1))

    def test_an_l2_finding_after_the_completion_does_not_unclassify_it(self):
        sha = self.commit("one\n")
        finding = {"body": "## L2 Finding\n\nsomething", "created_at": "2020-01-02T00:00:00Z"}
        self.assertIsNone(self.report(sha, comments_extra=[finding]))


if __name__ == "__main__":
    unittest.main()
