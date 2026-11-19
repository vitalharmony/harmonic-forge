"""harmonic-forge#1416: the branch-ahead notice after a Lane 2 completion.

Real temporary repositories (a bare origin, a lane clone and a second clone that
advances main), real `commit --amend` and `rebase --autosquash`; no mocked git.
Only the two GitHub reads are patched: `_issue_meta` and `_fetch_all_comments`.
"""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import l2_post  # noqa: E402
import watch_lane_posts as wlp  # noqa: E402

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}


def git(cwd: Path, *args: str, env: dict | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                            env={**os.environ, **GIT_ENV, **(env or {})})
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def completion(head: str | None) -> dict:
    body = l2_post.compose_body("completion", [], "done", lead={
        "Status": "done", "Change": "x", "Next": "Lane 1 to review"}, head=head)
    return {"body": body, "created_at": "2026-11-18T00:00:00Z"}


class Repos(unittest.TestCase):
    """origin (bare), lane (the Lane 2 worktree) and other (advances main)."""

    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.origin, self.lane, self.other = root / "origin.git", root / "lane", root / "other"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        git(root, "clone", "-q", str(self.origin), str(self.lane))
        lines = "".join(f"line {i}\n" for i in range(1, 21))
        (self.lane / "a.txt").write_text(lines)
        git(self.lane, "add", "a.txt")
        git(self.lane, "commit", "-qm", "base")
        git(self.lane, "push", "-q", "origin", "HEAD:main")
        git(root, "clone", "-q", str(self.origin), str(self.other))
        git(self.lane, "checkout", "-qb", "fix/1-x")

    def edit(self, repo: Path, name: str, old: str, new: str) -> None:
        path = repo / name
        path.write_text(path.read_text().replace(old, new))

    def commit(self, repo: Path, message: str) -> str:
        git(repo, "commit", "-qam", message)
        return git(repo, "rev-parse", "HEAD")

    def advance_main(self, old: str, new: str) -> None:
        git(self.other, "pull", "-q", "origin", "main")
        self.edit(self.other, "a.txt", old, new)
        self.commit(self.other, "main moves")
        git(self.other, "push", "-q", "origin", "HEAD:main")
        git(self.lane, "fetch", "-q", "origin")

    def notice(self, comments: list[dict]) -> str | None:
        with mock.patch.object(wlp, "_issue_meta", return_value=("open", set(), None)), \
             mock.patch.object(wlp, "_fetch_all_comments", return_value=comments):
            return wlp.branch_ahead_without_completion(str(self.lane), "o/r", 1)

    def reported(self) -> str:
        """One branch commit, reported by a completion; returns its SHA."""
        self.edit(self.lane, "a.txt", "line 10\n", "line 10 changed\n")
        return self.commit(self.lane, "work")


class FooterTests(unittest.TestCase):
    """AC1: only a completion carries head=, and only a 40-hex value."""

    def test_completion_carries_head_and_other_kinds_do_not(self) -> None:
        sha = "a" * 40
        self.assertIn(f"posted-by=LANE2; head={sha} -->",
                      l2_post.compose_body("completion", [], "x", lead={}, head=sha))
        for kind in ("plan", "blocked", "finding"):
            self.assertNotIn("head=", l2_post.compose_body(kind, [], "x", lead={}, head=sha))

    def test_a_non_sha_head_is_not_stamped(self) -> None:
        self.assertNotIn("head=", l2_post.compose_body("completion", [], "x", lead={}, head="abc"))

    def test_the_watcher_reads_head_from_the_footer_not_the_narrative(self) -> None:
        body = l2_post.compose_body("completion", [], "see package@" + "d" * 40, lead={},
                                    head="e" * 40)
        self.assertEqual(wlp._completion_head(body), "e" * 40)
        self.assertIsNone(wlp._completion_head(l2_post.compose_body("completion", [], "x", lead={})))


class PostingHeadTests(Repos):
    """AC1: the head comes from the posting worktree, never the process cwd."""

    def setUp(self) -> None:
        super().setUp()
        git(self.lane, "remote", "set-url", "origin", "https://github.com/o/r.git")
        self.head = self.reported()

    def test_the_mise_wrapper_shape_reads_the_caller_worktree(self) -> None:
        elsewhere = Path(tempfile.mkdtemp())
        git(elsewhere, "init", "-q")
        cwd = os.getcwd()
        os.chdir(elsewhere)
        self.addCleanup(os.chdir, cwd)
        with mock.patch.dict(os.environ, {"HF_CALLER_CWD": str(self.lane)}):
            self.assertEqual(l2_post.posting_head(os.environ["HF_CALLER_CWD"], "o/r"), self.head)

    def test_main_defaults_worktree_to_hf_caller_cwd_before_it_is_popped(self) -> None:
        narrative = Path(tempfile.mkdtemp()) / "n.md"
        narrative.write_text("done")
        posted = {}
        argv = ["l2_post.py", "post", "--kind", "completion", "--repo", "o/r", "--issue", "1",
                "--narrative-file", str(narrative), "--status", "s", "--change", "c",
                "--next", "Lane 1 to review"]
        elsewhere = Path(tempfile.mkdtemp())
        cwd = os.getcwd()
        os.chdir(elsewhere)
        self.addCleanup(os.chdir, cwd)
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.dict(os.environ, {"HF_CALLER_CWD": str(self.lane)}), \
             mock.patch.object(l2_post, "apply_project_identity"), \
             mock.patch.object(l2_post, "lock_blocks", return_value=False), \
             mock.patch.object(l2_post, "require_real_run"), \
             mock.patch.object(l2_post.orphan_report, "report"), \
             mock.patch.object(l2_post, "post", side_effect=lambda r, i, b: posted.update(body=b)), \
             mock.patch.object(l2_post, "_caller_path") as caller, \
             contextlib.suppress(Exception):
            caller.CALLER_CWD = "HF_CALLER_CWD"
            caller.resolve_args.side_effect = lambda *a, **k: os.environ.pop("HF_CALLER_CWD", None)
            l2_post.main()
        self.assertIn(f"head={self.head}", posted.get("body", ""))

    def test_a_worktree_of_another_repo_omits_head_and_warns(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertIsNone(l2_post.posting_head(str(self.lane), "o/other"))
        self.assertEqual(err.getvalue().count("\n"), 1)


class SinceCompletionTests(Repos):
    def test_new_commits_announce_with_their_count(self) -> None:  # AC4
        reported = self.reported()
        self.edit(self.lane, "a.txt", "line 2\n", "line 2 x\n")
        self.commit(self.lane, "more")
        self.edit(self.lane, "a.txt", "line 3\n", "line 3 x\n")
        self.commit(self.lane, "more again")
        self.assertIn("2 commits after its last completion", self.notice([completion(reported)]))

    def test_a_merge_up_of_main_counts_only_first_parent(self) -> None:  # AC4 / 5d
        reported = self.reported()
        self.advance_main("line 18\n", "line 18 main\n")
        git(self.lane, "merge", "-q", "--no-edit", "origin/main")
        self.assertIn("1 commit after its last completion", self.notice([completion(reported)]))

    def test_a_content_amend_announces(self) -> None:  # AC2
        reported = self.reported()
        self.edit(self.lane, "a.txt", "line 10 changed\n", "line 10 changed again\n")
        git(self.lane, "commit", "-q", "--amend", "--no-edit", "-a")
        self.assertIn("rework since its last completion", self.notice([completion(reported)]))

    def test_a_whitespace_only_amend_announces(self) -> None:  # AC2
        reported = self.reported()
        self.edit(self.lane, "a.txt", "line 10 changed\n", "line 10 changed  \n")
        git(self.lane, "commit", "-q", "--amend", "--no-edit", "-a")
        self.assertIn("rework", self.notice([completion(reported)]))

    def test_a_fixup_autosquash_announces(self) -> None:  # AC3
        self.edit(self.lane, "a.txt", "line 10\n", "line 10 changed\n")
        reported = self.commit(self.lane, "work")
        self.edit(self.lane, "a.txt", "line 11\n", "line 11 fixed\n")
        git(self.lane, "commit", "-q", "-a", "--fixup", reported)
        git(self.lane, "rebase", "-q", "-i", "--autosquash", "origin/main",
            env={"GIT_SEQUENCE_EDITOR": "true"})
        self.assertIn("rework", self.notice([completion(reported)]))

    def test_a_clean_rebase_over_a_changed_context_line_is_silent(self) -> None:  # AC5
        reported = self.reported()
        self.advance_main("line 8\n", "line 8 main\n")  # a context line of the branch's hunk
        git(self.lane, "rebase", "-q", "origin/main")
        self.assertIsNone(self.notice([completion(reported)]))

    def test_a_clean_rebase_of_a_three_commit_branch_is_silent(self) -> None:  # AC5 / 5b
        for n in (5, 10, 15):
            self.edit(self.lane, "a.txt", f"line {n}\n", f"line {n} b\n")
            reported = self.commit(self.lane, f"c{n}")
        self.advance_main("line 1\n", "line 1 main\n")
        git(self.lane, "rebase", "-q", "origin/main")
        self.assertIsNone(self.notice([completion(reported)]))

    def test_a_hand_resolved_conflict_rebase_announces(self) -> None:  # AC5 / 5c
        reported = self.reported()
        self.advance_main("line 10\n", "line 10 main\n")
        rebase = subprocess.run(["git", "rebase", "-q", "origin/main"], cwd=self.lane,
                                capture_output=True, text=True, env={**os.environ, **GIT_ENV})
        self.assertNotEqual(rebase.returncode, 0)
        lines = "".join(f"line {i}\n" for i in range(1, 21)).replace("line 10\n", "line 10 resolved\n")
        (self.lane / "a.txt").write_text(lines)
        git(self.lane, "add", "a.txt")
        git(self.lane, "rebase", "--continue", env={"GIT_EDITOR": "true"})
        self.assertIn("rework", self.notice([completion(reported)]))

    def test_a_message_only_amend_is_silent(self) -> None:  # AC6
        reported = self.reported()
        git(self.lane, "commit", "-q", "--amend", "-m", "reworded")
        self.assertIsNone(self.notice([completion(reported)]))

    def test_the_reported_head_itself_is_silent(self) -> None:
        self.assertIsNone(self.notice([completion(self.reported())]))


class SilentFailureTests(Repos):  # AC7
    def test_a_completion_without_head_is_silent(self) -> None:
        self.reported()
        self.edit(self.lane, "a.txt", "line 2\n", "line 2 x\n")
        self.commit(self.lane, "more")
        self.assertIsNone(self.notice([completion(None)]))

    def test_a_missing_reported_object_is_silent(self) -> None:
        self.reported()
        self.assertIsNone(self.notice([completion("f" * 40)]))

    def test_a_missing_origin_main_is_silent(self) -> None:
        reported = self.reported()
        git(self.lane, "commit", "-q", "--amend", "--no-edit", "--allow-empty", "-m", "x2",
            env={"GIT_COMMITTER_DATE": "2001-01-01T00:00:00"})
        git(self.lane, "update-ref", "-d", "refs/remotes/origin/main")
        self.assertIsNone(wlp._since_completion(str(self.lane), reported))

    def test_no_git_on_path_is_silent_and_never_raises(self) -> None:
        reported = self.reported()
        with mock.patch.dict(os.environ, {"PATH": str(Path(tempfile.mkdtemp()))}):
            self.assertIsNone(wlp._run_git(str(self.lane), "rev-parse", "HEAD"))
            self.assertIsNone(wlp._is_ancestor(str(self.lane), reported, "HEAD"))
            self.assertIsNone(wlp._since_completion(str(self.lane), reported))


class RegressionTests(Repos):  # AC8
    def test_no_completion_still_announces(self) -> None:
        self.reported()
        self.assertIn("no completion posted", self.notice([]))

    def test_tooling_exception_still_suppresses(self) -> None:
        reported = self.reported()
        self.edit(self.lane, "a.txt", "line 2\n", "line 2 x\n")
        self.commit(self.lane, "more")
        with mock.patch.object(wlp, "_issue_meta", return_value=("open", {"tooling-exception"}, None)), \
             mock.patch.object(wlp, "_fetch_all_comments", return_value=[completion(reported)]):
            self.assertIsNone(wlp.branch_ahead_without_completion(str(self.lane), "o/r", 1))

    def test_a_finding_after_the_completion_does_not_unclassify_it(self) -> None:
        reported = self.reported()
        finding = {"body": l2_post.compose_body("finding", [], "a defect", lead={})}
        self.assertIsNone(self.notice([completion(reported), finding]))


if __name__ == "__main__":
    unittest.main()
