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


if __name__ == "__main__":
    unittest.main()
