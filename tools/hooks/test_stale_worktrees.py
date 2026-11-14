"""harmonic-forge#1362: tests for the post-merge stale-worktree warning.

Temporary git repos stand in for lane worktrees; a fake runner answers every
`gh` call and passes `git` through. Nothing reads the real root or the network."""
from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch_consume  # noqa: E402
import stale_worktrees as sw  # noqa: E402

REPO = "vitalharmony/hrse"


def git(path: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


class FakeRunner:
    """Answers `gh` calls from a table; runs `git` for real."""

    def __init__(self, main_sha: str, pulls: dict[str, tuple[int, str]] | None = None):
        self.main_sha = main_sha
        self.pulls = pulls or {}
        self.gh_calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        if cmd[0] != "gh":
            return subprocess.run(cmd, **kwargs)
        self.gh_calls.append(cmd)
        path = cmd[2]
        if path.endswith("/branches/main"):
            return subprocess.CompletedProcess(cmd, 0, self.main_sha + "\n", "")
        head = path.split("/commits/")[1].split("/")[0]
        code, body = self.pulls.get(head, (0, "[]"))
        if code == 422:
            return subprocess.CompletedProcess(cmd, 1, "", "gh: No commit found (HTTP 422)")
        return subprocess.CompletedProcess(cmd, code, body, "")


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin"
        self.origin.mkdir()
        git(self.origin, "init", "-q", "-b", "main")
        git(self.origin, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
            "--allow-empty", "-m", "base")
        self.base = git(self.origin, "rev-parse", "HEAD")
        self.root = self.tmp / "worktrees"
        self.root.mkdir()

    def clone(self, name: str, url: str = f"https://github.com/{REPO}.git") -> Path:
        path = self.root / name
        subprocess.run(["git", "clone", "-q", str(self.origin), str(path)], check=True)
        git(path, "remote", "set-url", "origin", url)
        return path

    def advance_main(self) -> str:
        git(self.origin, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
            "--allow-empty", "-m", "merged")
        return git(self.origin, "rev-parse", "HEAD")

    def deadline(self, seconds: float = 15.0) -> float:
        return time.monotonic() + seconds


class StaleScan(Fixture):
    def test_1_a_behind_worktree_with_no_pr_is_named(self):
        self.clone("hrse2-1-impl")
        new = self.advance_main()
        found, unchecked = sw.stale_worktrees(REPO, new, self.root, self.deadline(), FakeRunner(new))
        self.assertEqual([f["dir"] for f in found], ["hrse2-1-impl"])
        self.assertEqual(found[0]["label"], "no PR")
        self.assertEqual(unchecked, 0)

    def test_1b_a_fetched_head_that_is_not_an_ancestor_is_behind(self):
        """AC2's second clause: the new head's object is present (fetched) but HEAD
        is a diverged local commit, so the cat-file check alone cannot decide."""
        path = self.clone("hrse2-1b-impl")
        git(path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
            "--allow-empty", "-m", "local work")
        new = self.advance_main()
        git(path, "remote", "set-url", "origin", str(self.origin))
        git(path, "fetch", "-q", "origin")
        git(path, "remote", "set-url", "origin", f"https://github.com/{REPO}.git")
        git(path, "cat-file", "-e", f"{new}^{{commit}}")  # present, so the ancestor path decides
        found, _ = sw.stale_worktrees(REPO, new, self.root, self.deadline(), FakeRunner(new))
        self.assertEqual([f["dir"] for f in found], ["hrse2-1b-impl"])

    def test_2_a_head_in_a_closed_pr_is_skipped(self):
        path = self.clone("hrse2-2-impl")
        new = self.advance_main()
        head = git(path, "rev-parse", "HEAD")
        runner = FakeRunner(new, {head: (0, json.dumps([{"number": 9, "state": "closed"}]))})
        found, _ = sw.stale_worktrees(REPO, new, self.root, self.deadline(), runner)
        self.assertEqual(found, [])

    def test_3_a_worktree_containing_the_new_head_is_not_named(self):
        new = self.advance_main()
        self.clone("hrse2-3-impl")
        found, _ = sw.stale_worktrees(REPO, new, self.root, self.deadline(), FakeRunner(new))
        self.assertEqual(found, [])

    def test_4_a_worktree_on_another_repo_is_not_named(self):
        self.clone("forge-4-impl", "https://github.com/vitalharmony/harmonic-forge.git")
        new = self.advance_main()
        found, unchecked = sw.stale_worktrees(REPO, new, self.root, self.deadline(), FakeRunner(new))
        self.assertEqual((found, unchecked), ([], 0))

    def test_9_an_unpushed_head_answered_422_is_named_unpushed(self):
        path = self.clone("hrse2-9-impl")
        new = self.advance_main()
        runner = FakeRunner(new, {git(path, "rev-parse", "HEAD"): (422, "")})
        found, _ = sw.stale_worktrees(REPO, new, self.root, self.deadline(), runner)
        self.assertEqual(found[0]["label"], "no PR (unpushed)")

    def test_open_pr_is_named_with_its_number(self):
        path = self.clone("hrse2-o-impl")
        new = self.advance_main()
        runner = FakeRunner(new, {git(path, "rev-parse", "HEAD"):
                                  (0, json.dumps([{"number": 7, "state": "open"}]))})
        found, _ = sw.stale_worktrees(REPO, new, self.root, self.deadline(), runner)
        self.assertEqual(found[0]["label"], "PR #7 open")

    def test_10_a_spent_deadline_counts_unchecked_and_still_reports(self):
        self.clone("hrse2-10-impl")
        new = self.advance_main()
        found, unchecked = sw.stale_worktrees(REPO, new, self.root, time.monotonic() - 1,
                                              FakeRunner(new))
        self.assertEqual(found, [])
        self.assertEqual(unchecked, 1)
        self.assertIn("1 worktree(s) were not checked", sw.message(REPO, new, found, unchecked))

    def test_11_an_unreadable_origin_is_unchecked_not_another_repo(self):
        path = self.clone("hrse2-11-impl")
        git(path, "remote", "remove", "origin")
        new = self.advance_main()
        found, unchecked = sw.stale_worktrees(REPO, new, self.root, self.deadline(), FakeRunner(new))
        self.assertEqual((found, unchecked), ([], 1))


class Warn(Fixture):
    def test_5_a_merge_that_did_not_land_says_nothing(self):
        self.clone("hrse2-5-impl")
        new = self.advance_main()
        with mock.patch.object(sw.batch_auth, "action_landed", return_value=None):
            self.assertIsNone(sw.warn(f"gh pr merge 5 --repo {REPO} --squash", None,
                                      self.root, FakeRunner(new)))

    def test_6_a_non_merge_command_makes_no_call(self):
        runner = FakeRunner("0" * 40)
        self.assertIsNone(sw.warn("git status", None, self.root, runner))
        self.assertEqual(runner.gh_calls, [])

    def test_7_a_bare_merge_takes_its_repo_from_cwd(self):
        self.clone("hrse2-7-impl")
        new = self.advance_main()
        with mock.patch.object(sw.batch_auth, "action_landed", return_value=True), \
                mock.patch.object(sw.batch_auth, "cwd_repo", return_value=REPO) as cwd_repo:
            text = sw.warn("gh pr merge 7 --squash", "/some/checkout", self.root, FakeRunner(new))
        cwd_repo.assert_called_once()
        self.assertIn("hrse2-7-impl", text)

    def test_13_a_failed_main_read_says_nothing(self):
        self.clone("hrse2-13-impl")
        self.advance_main()

        def runner(cmd, **kwargs):
            if cmd[0] == "gh":
                return subprocess.CompletedProcess(cmd, 1, "", "boom")
            return subprocess.run(cmd, **kwargs)

        with mock.patch.object(sw.batch_auth, "action_landed", return_value=True):
            self.assertIsNone(sw.warn(f"gh pr merge 13 --repo {REPO}", None, self.root, runner))


class HookWiring(unittest.TestCase):
    def _main(self, warn_side_effect=None, warn_value=None):
        payload = {"tool_name": "Bash", "tool_input": {"command": "gh pr merge 1 --repo x/y"},
                   "cwd": "/c"}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(batch_consume.batch_auth, "consume") as consume, \
                mock.patch.object(batch_consume.batch_auth, "has_close_or_merge", return_value=True), \
                mock.patch.object(batch_consume.batch_auth, "prune_now") as prune, \
                mock.patch.object(batch_consume, "_retire_closed_imc"), \
                mock.patch.object(sw, "warn", side_effect=warn_side_effect, return_value=warn_value), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                redirect_stdout(out), redirect_stderr(err):
            batch_consume.main()
        return out.getvalue(), err.getvalue(), consume, prune

    def test_8_an_exception_is_one_stderr_line_and_the_bookkeeping_still_ran(self):
        out, err, consume, prune = self._main(warn_side_effect=RuntimeError("x"))
        self.assertEqual(out, "")
        self.assertEqual(len(err.strip().splitlines()), 1)
        consume.assert_called_once()
        prune.assert_called_once()

    def test_12_stdout_is_exactly_one_json_object(self):
        out, _, _, _ = self._main(warn_value="main moved")
        parsed = json.loads(out)
        self.assertEqual(parsed["hookSpecificOutput"]["additionalContext"], "main moved")
        self.assertEqual(parsed["hookSpecificOutput"]["hookEventName"], "PostToolUse")


if __name__ == "__main__":
    unittest.main()
