#!/usr/bin/env python3
"""Unit tests for block_lane1_status_claims.py's write_on_main_branch()
(harmonic-forge#384) -- the identity-independent main-checkout write guard.

Uses real temporary git repos rather than mocked subprocess calls: the
behavior under test is entirely git-state-dependent (which branch is
checked out, whether a path is tracked), so a real `git init`/`checkout`/
`add` sequence is a more honest oracle than a hand-maintained mock of
`git branch --show-current` and `git ls-files` output shapes.

Run: python3 tools/hooks/test_write_on_main_branch.py
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import block_lane1_status_claims as m


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], text=True, capture_output=True, check=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )


class _RepoTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        _git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "tracked.txt").write_text("v1\n")
        _git(self.repo, "add", "tracked.txt")
        _git(self.repo, "commit", "-q", "-m", "seed")

    def tearDown(self) -> None:
        self._tmp.cleanup()


class MainBranchTrackedFileTests(_RepoTestCase):
    def test_tracked_file_on_main_is_blocked(self) -> None:
        self.assertTrue(m.write_on_main_branch(str(self.repo / "tracked.txt"), self.repo))

    def test_untracked_file_on_main_is_allowed(self) -> None:
        """AC3: untracked files are never blocked, on any branch."""
        scratch = self.repo / "scratch.txt"
        scratch.write_text("not added\n")
        self.assertFalse(m.write_on_main_branch(str(scratch), self.repo))

    def test_new_file_that_does_not_exist_yet_is_allowed(self) -> None:
        """A Write call creating a brand-new file -- the path doesn't
        exist at check time, so `git ls-files --error-unmatch` reports
        untracked. Matches AC3's intent: a new file isn't the violation
        this guard exists to stop."""
        brand_new = self.repo / "not-created-yet.txt"
        self.assertFalse(brand_new.exists())
        self.assertFalse(m.write_on_main_branch(str(brand_new), self.repo))

    def test_tracked_file_on_a_feature_branch_is_allowed(self) -> None:
        """AC2: on any non-main branch, behavior is unchanged (allowed)."""
        _git(self.repo, "checkout", "-q", "-b", "feature/x")
        self.assertFalse(m.write_on_main_branch(str(self.repo / "tracked.txt"), self.repo))

    def test_detached_head_at_mains_commit_is_allowed(self) -> None:
        """AC5: a detached HEAD is not treated as `main`, even when it is
        AT main's own commit -- `git branch --show-current` returns the
        empty string on a detached HEAD, never the literal 'main'. This
        is the Lane 3 gate-checkout fallback shape."""
        sha = _git(self.repo, "rev-parse", "main").stdout.strip()
        _git(self.repo, "checkout", "-q", sha)
        branch = _git(self.repo, "branch", "--show-current").stdout.strip()
        self.assertEqual(branch, "", "detached HEAD must report no branch name")
        self.assertFalse(m.write_on_main_branch(str(self.repo / "tracked.txt"), self.repo))

    def test_relative_file_path_resolved_against_cwd(self) -> None:
        self.assertTrue(m.write_on_main_branch("tracked.txt", self.repo))

    def test_symlink_into_a_different_main_branch_repo_is_blocked(self) -> None:
        """harmonic-forge#384 preclose review (silent-bypass + fail-direction
        lenses, live-reproduced independently): a checked-in symlink whose
        own directory is a feature-branch (or untracked-there) checkout,
        but whose TARGET is a tracked file in a second repo that has
        `main` checked out, must still be blocked. This is the exact shape
        of HRSE2's `.claude/rules/backend-python.md` -> ~/harmonic-forge
        symlinks. `lane2_write_in_main_checkout` above already defends
        against this by checking both the lexical and resolved path; this
        guard must too."""
        with tempfile.TemporaryDirectory() as other_tmp:
            other_repo = Path(other_tmp)
            _git(other_repo, "init", "-q", "-b", "main")
            (other_repo / "rule.md").write_text("v1\n")
            _git(other_repo, "add", "rule.md")
            _git(other_repo, "commit", "-q", "-m", "seed")

            _git(self.repo, "checkout", "-q", "-b", "feature/x")
            symlink_dir = self.repo / "linked"
            symlink_dir.mkdir()
            symlink_path = symlink_dir / "rule.md"
            symlink_path.symlink_to(other_repo / "rule.md")

            self.assertTrue(
                m.write_on_main_branch(str(symlink_path), self.repo),
                "a write through the symlink reaches a tracked file in a "
                "DIFFERENT repo that has main checked out -- must be blocked "
                "even though the symlink's own directory is on a feature "
                "branch",
            )

    def test_empty_file_path_is_allowed(self) -> None:
        self.assertFalse(m.write_on_main_branch("", self.repo))

    def test_path_outside_any_git_repo_is_allowed(self) -> None:
        """Fails open, matching this file's non-adversarial posture
        elsewhere -- confirmed via a path whose directory is not a git
        repo at all (git -C <dir> branch --show-current exits nonzero)."""
        with tempfile.TemporaryDirectory() as outside:
            self.assertFalse(m.write_on_main_branch(str(Path(outside) / "x.txt"), Path(outside)))


class MainHookIntegrationTests(_RepoTestCase):
    """AC1/AC4, exercised through main()'s actual EDIT_WRITE_TOOLS branch,
    not write_on_main_branch() in isolation -- confirms the function is
    actually wired in, not just correct on its own."""

    def _payload(self, tool_name: str, file_path: str) -> dict:
        return {"tool_name": tool_name, "cwd": str(self.repo), "tool_input": {"file_path": file_path}}

    def test_edit_denied_on_main_with_no_lane_set(self) -> None:
        """AC1: fires for every session including one with no LANE set."""
        os.environ.pop("LANE", None)
        payload = self._payload("Edit", str(self.repo / "tracked.txt"))
        out = io.StringIO()
        with unittest.mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
             contextlib.redirect_stdout(out):
            m.main()
        result = json.loads(out.getvalue())
        self.assertEqual(result.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")
        self.assertIn("git checkout -b", result["hookSpecificOutput"]["permissionDecisionReason"])

    def test_lane2_denial_still_fires_independently_on_a_feature_branch(self) -> None:
        """AC4, unit-level: the pre-existing LANE=2 main-checkout denial is
        unchanged -- still fires on its own condition (a feature-branch
        worktree whose directory name matches the LANE=2 main-checkout-root
        resolution), independent of this new branch-name check."""
        _git(self.repo, "checkout", "-q", "-b", "feature/x")
        os.environ["LANE"] = "2"
        try:
            result_on_feature_branch = m.lane2_write_in_main_checkout(
                str(self.repo / "tracked.txt"), self.repo
            )
        finally:
            os.environ.pop("LANE", None)
        # lane2_write_in_main_checkout only fires when the resolved main
        # checkout root matches -- this repo IS its own "main checkout"
        # root (no -lane2 suffix stripped), so it should still deny here
        # regardless of which branch happens to be checked out.
        self.assertTrue(result_on_feature_branch)

    def test_lane2_denial_wins_through_main_when_both_conditions_are_true(self) -> None:
        """AC4, through main()'s actual combined dispatch, not the guard
        functions called in isolation -- harmonic-forge#384 preclose review
        (fail-direction lens) found the prior version of this test could
        not detect main() checking the guards in the wrong order: LANE=2
        AND main checked out AND a tracked file is the one state where
        BOTH new-guard and old-guard conditions are simultaneously true,
        and it's the state a LANE=2 session in the main checkout is
        actually in. The LANE=2-specific remedy (restart in the -lane2
        worktree) must be what the session sees, not the generic
        branch-first message -- reordering the two checks would silently
        swap which message fires while every existing per-function test
        stayed green."""
        os.environ["LANE"] = "2"
        try:
            payload = self._payload("Edit", str(self.repo / "tracked.txt"))
            out = io.StringIO()
            with unittest.mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                 contextlib.redirect_stdout(out):
                m.main()
        finally:
            os.environ.pop("LANE", None)
        result = json.loads(out.getvalue())
        reason = result.get("hookSpecificOutput", {}).get("permissionDecisionReason", "")
        self.assertEqual(result.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")
        self.assertIn("harmonic-forge#142", reason, "the LANE=2-specific remedy must win, not the generic one")


if __name__ == "__main__":
    unittest.main()
