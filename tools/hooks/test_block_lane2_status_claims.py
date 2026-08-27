#!/usr/bin/env python3
"""Unit tests for block_lane2_status_claims.py (harmonic-forge#371).

Run: python3 tools/hooks/test_block_lane2_status_claims.py
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import block_lane2_status_claims as m


def _is_denied(result: dict) -> bool:
    return result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


class TestLane2RawPostDenial(unittest.TestCase):
    def setUp(self):
        self._prior_lane = os.environ.get("LANE")
        os.environ["LANE"] = "2"
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()
        if self._prior_lane is None:
            os.environ.pop("LANE", None)
        else:
            os.environ["LANE"] = self._prior_lane

    def test_gh_issue_comment_denied_under_lane2(self):
        result = m.decision("gh issue comment 371 --body hi", self.cwd)
        self.assertTrue(_is_denied(result))

    def test_gh_issue_create_denied_under_lane2(self):
        result = m.decision('gh issue create --title t --body b', self.cwd)
        self.assertTrue(_is_denied(result))

    def test_gh_api_post_comment_denied_under_lane2(self):
        result = m.decision(
            'gh api --method POST repos/vitalharmony/harmonic-forge/issues/371/comments -f body=hi',
            self.cwd,
        )
        self.assertTrue(_is_denied(result))

    def test_gh_api_post_comment_denied_under_lane2_second_repo(self):
        """AC's repo-agnostic requirement, provably tested across repos,
        not just stated: same denial for a completely different repo."""
        result = m.decision(
            'gh api --method POST repos/vitalharmony/hrse/issues/1219/comments -f body=hi',
            self.cwd,
        )
        self.assertTrue(_is_denied(result))

    def test_gh_issue_edit_body_denied_under_lane2(self):
        result = m.decision("gh issue edit 371 --body updated", self.cwd)
        self.assertTrue(_is_denied(result))

    def test_mise_gh_new_issue_denied_under_lane2(self):
        """harmonic-forge#388: the sanctioned Lane 1 filing path was the
        actual hole -- `gh issue create` alone let this through undetected."""
        result = m.decision(
            "mise run gh-new-issue --title t --labels bug --milestone Later",
            self.cwd,
        )
        self.assertTrue(_is_denied(result))

    def test_gh_issue_py_direct_invocation_denied_under_lane2(self):
        result = m.decision("python3 tools/gh/gh_issue.py --title t", self.cwd)
        self.assertTrue(_is_denied(result))

    def test_gh_new_issue_not_denied_for_lane1(self):
        """The check is Lane-2-specific -- Lane 1's own sanctioned filing
        path must never be denied by this hook."""
        os.environ["LANE"] = "1"
        result = m.decision("mise run gh-new-issue --title t", self.cwd)
        self.assertFalse(_is_denied(result))

    def test_l2_post_py_itself_is_not_a_recognized_transport_shape(self):
        """The wrapper's own invocation must never match the deny pattern --
        it is a different script name/shape than every recognized transport,
        so no explicit allowlist carve-out is needed (mirrors Lane 1's design,
        which never special-cases l1_post.py either)."""
        result = m.decision(
            'python3 tools/gh/l2_post.py post --kind completion --repo '
            'vitalharmony/harmonic-forge --issue 371 --narrative-file n.txt',
            self.cwd,
        )
        self.assertFalse(_is_denied(result))

    def test_mise_run_l2_post_is_not_denied(self):
        result = m.decision("mise run l2-post --kind plan --issue 371 "
                             "--narrative-file n.txt", self.cwd)
        self.assertFalse(_is_denied(result))

    def test_unrelated_command_allowed(self):
        result = m.decision("git status", self.cwd)
        self.assertFalse(_is_denied(result))

    def test_no_denial_when_lane_unset(self):
        """This hook is Lane-2-specific -- LANE unset is out of its scope
        (Lane 1's own guard covers that case)."""
        os.environ.pop("LANE", None)
        result = m.decision("gh issue comment 371 --body hi", self.cwd)
        self.assertFalse(_is_denied(result))

    def test_no_denial_for_lane1(self):
        os.environ["LANE"] = "1"
        result = m.decision("gh issue comment 371 --body hi", self.cwd)
        self.assertFalse(_is_denied(result))

    def test_no_denial_for_lane3(self):
        os.environ["LANE"] = "3"
        result = m.decision("gh issue comment 371 --body hi", self.cwd)
        self.assertFalse(_is_denied(result))

    def test_cd_prefixed_command_still_recognized(self):
        result = m.decision("cd /tmp && gh issue comment 371 --body hi", self.cwd)
        self.assertTrue(_is_denied(result))

    def test_malformed_payload_command_denies_closed(self):
        result = m.decision(None, self.cwd)
        self.assertTrue(_is_denied(result))


class TestLane2PushAndPrCreateDenial(unittest.TestCase):
    """harmonic-forge#398 -- feedback_lane2_never_pushes_or_prs."""

    def setUp(self):
        self._prior_lane = os.environ.get("LANE")
        os.environ["LANE"] = "2"
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()
        if self._prior_lane is None:
            os.environ.pop("LANE", None)
        else:
            os.environ["LANE"] = self._prior_lane

    def test_git_push_denied_under_lane2(self):
        result = m.decision("git push origin feat/398-fix", self.cwd)
        self.assertTrue(_is_denied(result))

    def test_git_push_with_dash_c_still_recognized(self):
        result = m.decision("git -C /tmp/impl push origin feat/398-fix", self.cwd)
        self.assertTrue(_is_denied(result))

    def test_gh_pr_create_denied_under_lane2(self):
        result = m.decision('gh pr create --title t --body b', self.cwd)
        self.assertTrue(_is_denied(result))

    def test_git_push_not_denied_when_lane_unset(self):
        os.environ.pop("LANE", None)
        result = m.decision("git push origin feat/398-fix", self.cwd)
        self.assertFalse(_is_denied(result))

    def test_git_push_not_denied_for_lane1(self):
        os.environ["LANE"] = "1"
        result = m.decision("git push origin feat/398-fix", self.cwd)
        self.assertFalse(_is_denied(result))

    def test_git_push_not_denied_for_lane3(self):
        os.environ["LANE"] = "3"
        result = m.decision("git push origin feat/398-fix", self.cwd)
        self.assertFalse(_is_denied(result))

    def test_gh_pr_create_not_denied_when_lane_unset(self):
        os.environ.pop("LANE", None)
        result = m.decision('gh pr create --title t --body b', self.cwd)
        self.assertFalse(_is_denied(result))

    def test_unrelated_git_command_not_denied(self):
        result = m.decision("git status", self.cwd)
        self.assertFalse(_is_denied(result))

    def test_gh_pr_view_not_denied(self):
        """Only pr create is denied -- read-only pr commands are unaffected."""
        result = m.decision("gh pr view 12", self.cwd)
        self.assertFalse(_is_denied(result))

    def test_gh_as_wrapped_pr_create_denied(self):
        """preclose-inspection finding: `gh-as <account> gh pr create ...`
        (rules/universal-agent.md's documented scoping wrapper) must be
        caught the same as the bare form."""
        result = m.decision('gh-as vitalharmony gh pr create --title t --body b', self.cwd)
        self.assertTrue(_is_denied(result))

    def test_gh_as_wrapped_push_denied(self):
        result = m.decision('gh-as vitalharmony git push origin feat/398-fix', self.cwd)
        self.assertTrue(_is_denied(result))

    def test_gh_as_wrapped_pr_create_not_denied_for_lane1(self):
        os.environ["LANE"] = "1"
        result = m.decision('gh-as vitalharmony gh pr create --title t --body b', self.cwd)
        self.assertFalse(_is_denied(result))

    def test_gh_as_unrelated_command_not_denied(self):
        result = m.decision('gh-as vitalharmony gh issue list', self.cwd)
        self.assertFalse(_is_denied(result))

    def test_mise_run_commit_push_denied(self):
        """preclose-inspection finding: HRSE2's own documented push path
        (CLAUDE.md) forwards to scripts/git_commit.py's internal `git
        push` -- the literal command never contains a `git push` token,
        only this flag."""
        result = m.decision("mise run commit --push", self.cwd)
        self.assertTrue(_is_denied(result))

    def test_mise_run_restart_push_denied(self):
        result = m.decision("mise run restart b --push", self.cwd)
        self.assertTrue(_is_denied(result))

    def test_mise_run_commit_without_push_not_denied(self):
        result = m.decision('mise run commit --message "wip"', self.cwd)
        self.assertFalse(_is_denied(result))

    def test_mise_run_restart_without_push_not_denied(self):
        result = m.decision("mise run restart --no-git", self.cwd)
        self.assertFalse(_is_denied(result))

    def test_mise_run_commit_push_not_denied_for_lane1(self):
        os.environ["LANE"] = "1"
        result = m.decision("mise run commit --push", self.cwd)
        self.assertFalse(_is_denied(result))

    def test_mise_run_other_task_not_denied(self):
        result = m.decision("mise run l1-post --push", self.cwd)
        self.assertFalse(_is_denied(result))

    def test_mise_bare_task_spelling_push_denied(self):
        """preclose-inspection finding, round 2: `mise <task>` (no `run`)
        is an equally valid mise invocation and bypassed the original
        check, which only matched the `mise run <task>` spelling."""
        result = m.decision("mise restart --push", self.cwd)
        self.assertTrue(_is_denied(result))

    def test_mise_r_alias_spelling_push_denied(self):
        """`r` is mise's own documented alias for `run`."""
        result = m.decision("mise r restart --push", self.cwd)
        self.assertTrue(_is_denied(result))

    def test_mise_bare_task_spelling_without_push_not_denied(self):
        result = m.decision("mise restart --no-git", self.cwd)
        self.assertFalse(_is_denied(result))

    def test_direct_git_commit_py_invocation_with_push_denied(self):
        """preclose-inspection finding, round 2: the actual tool named in
        this function's own docstring, invoked directly rather than
        through any mise spelling, was never checked at all."""
        result = m.decision('python3 scripts/git_commit.py --message "wip" --push', self.cwd)
        self.assertTrue(_is_denied(result))

    def test_bare_git_commit_py_invocation_with_push_denied(self):
        result = m.decision("./scripts/git_commit.py --push", self.cwd)
        self.assertTrue(_is_denied(result))

    def test_direct_git_commit_py_invocation_without_push_not_denied(self):
        result = m.decision('python3 scripts/git_commit.py --message "wip"', self.cwd)
        self.assertFalse(_is_denied(result))

    def test_direct_git_commit_py_push_not_denied_for_lane1(self):
        os.environ["LANE"] = "1"
        result = m.decision("python3 scripts/git_commit.py --push", self.cwd)
        self.assertFalse(_is_denied(result))


if __name__ == "__main__":
    unittest.main()
