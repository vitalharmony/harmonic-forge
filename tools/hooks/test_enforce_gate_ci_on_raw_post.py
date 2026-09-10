#!/usr/bin/env python3
"""Unit tests for enforce_gate_ci_on_raw_post.py (harmonic-forge#565).

`check` is injected everywhere so no test ever reaches `gh api` — mirroring
`gate_ci.py`'s own `run=` injection pattern.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import enforce_gate_ci_on_raw_post as m  # noqa: E402


def _is_denied(result: dict) -> bool:
    return result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


PASS_BODY = "## Lane 3 Gate Results — H999\n\n**Verdict:** PASS\nHead-SHA: abc1234\n"
FAIL_BODY = "## Lane 3 Gate Results — H999\n\n**Verdict:** FAIL\nHead-SHA: abc1234\n"
NON_GATE_BODY = "Just an ordinary status update, nothing gate-shaped here.\n"


def ok_check(repo, body):
    return True, "[GATE] CI green"


def refuse_check(repo, body):
    return False, "[GATE] REFUSED: CI is RED"


class FindBodyAndRepoTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmpdir.name)
        self.file = self.cwd / "body.md"
        self.file.write_text(PASS_BODY, encoding="utf-8")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_gh_issue_comment_inline_body(self):
        args = ["gh", "issue", "comment", "9", "--repo", "vitalharmony/hrse",
                "--body", PASS_BODY]
        self.assertEqual(m.find_body_and_repo(args, self.cwd),
                         (PASS_BODY, "vitalharmony/hrse"))

    def test_gh_issue_comment_body_file(self):
        args = ["gh", "issue", "comment", "9", "--repo", "vitalharmony/hrse",
                "--body-file", str(self.file)]
        self.assertEqual(m.find_body_and_repo(args, self.cwd),
                         (PASS_BODY, "vitalharmony/hrse"))

    def test_gh_issue_comment_without_repo_is_skipped(self):
        """Never guesses a repo -- a credential-isolation-sensitive check
        must not act on an unresolved repo (mirrors batch_auth._repo_flag)."""
        args = ["gh", "issue", "comment", "9", "--body-file", str(self.file)]
        self.assertIsNone(m.find_body_and_repo(args, self.cwd))

    def test_post_comment_py_bare(self):
        args = ["post_comment.py", "--repo", "vitalharmony/harmonic-forge",
                "--issue", "9", "--file", str(self.file)]
        self.assertEqual(m.find_body_and_repo(args, self.cwd),
                         (PASS_BODY, "vitalharmony/harmonic-forge"))

    def test_post_comment_py_via_python3(self):
        args = ["python3", "tools/gh/post_comment.py", "--repo",
                "vitalharmony/harmonic-forge", "--issue", "9",
                "--file", str(self.file)]
        self.assertEqual(m.find_body_and_repo(args, self.cwd),
                         (PASS_BODY, "vitalharmony/harmonic-forge"))

    def test_mise_run_post_comment(self):
        args = ["mise", "run", "post-comment", "--", "--repo",
                "vitalharmony/harmonic-forge", "--issue", "9",
                "--file", str(self.file)]
        self.assertEqual(m.find_body_and_repo(args, self.cwd),
                         (PASS_BODY, "vitalharmony/harmonic-forge"))

    def test_mise_r_alias_post_comment(self):
        """Preclose finding: `mise r` (mise's own documented alias for
        `run`) bypassed the first draft, mirroring the identical gap
        `block_lane2_status_claims.py` already found and fixed once."""
        args = ["mise", "r", "post-comment", "--", "--repo",
                "vitalharmony/harmonic-forge", "--issue", "9",
                "--file", str(self.file)]
        self.assertEqual(m.find_body_and_repo(args, self.cwd),
                         (PASS_BODY, "vitalharmony/harmonic-forge"))

    def test_gh_as_wrapped_issue_comment(self):
        """Preclose finding: `gh-as <account> gh issue comment ...` is the
        MANDATED spelling in this house (rules/universal-agent.md), not an
        alternate one -- a check matching only bare `gh` missed it
        entirely."""
        args = ["gh-as", "vitalharmony", "gh", "issue", "comment", "9",
                "--repo", "vitalharmony/hrse", "--body-file", str(self.file)]
        self.assertEqual(m.find_body_and_repo(args, self.cwd),
                         (PASS_BODY, "vitalharmony/hrse"))

    def test_gh_as_wrapped_post_comment_py(self):
        args = ["gh-as", "vitalharmony", "post_comment.py", "--repo",
                "vitalharmony/hrse", "--issue", "9", "--file", str(self.file)]
        self.assertEqual(m.find_body_and_repo(args, self.cwd),
                         (PASS_BODY, "vitalharmony/hrse"))

    def test_post_comment_py_inline_body(self):
        """Preclose finding: post_comment.py's own argparse exposes --file
        and --body as a required mutually-exclusive pair; the first draft
        read only --file, silently missing half the route's real
        interface."""
        args = ["post_comment.py", "--repo", "vitalharmony/harmonic-forge",
                "--issue", "9", "--body", PASS_BODY]
        self.assertEqual(m.find_body_and_repo(args, self.cwd),
                         (PASS_BODY, "vitalharmony/harmonic-forge"))

    def test_post_lane_discussion_is_not_matched(self):
        """Deliberately excluded -- #504 already guards this route
        in-script; re-checking it here would just double the network call
        for the same answer."""
        args = ["python3", "scripts/post_lane_discussion.py", "--repo",
                "vitalharmony/hrse", "--issue", "9", "--file", str(self.file)]
        self.assertIsNone(m.find_body_and_repo(args, self.cwd))

    def test_an_unrelated_command_is_not_matched(self):
        self.assertIsNone(m.find_body_and_repo(["git", "status"], self.cwd))

    def test_an_unreadable_body_file_is_not_matched(self):
        args = ["gh", "issue", "comment", "9", "--repo", "vitalharmony/hrse",
                "--body-file", str(self.cwd / "nope.md")]
        self.assertIsNone(m.find_body_and_repo(args, self.cwd))


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _decide(self, command: str, check=None) -> dict:
        return m.decision(command, self.cwd, check=check)

    def test_a_refused_pass_is_denied(self):
        cmd = f'gh issue comment 9 --repo vitalharmony/hrse --body "{PASS_BODY}"'
        result = self._decide(cmd, check=refuse_check)
        self.assertTrue(_is_denied(result))
        self.assertIn("REFUSED", result["systemMessage"])

    def test_a_green_pass_is_allowed(self):
        cmd = f'gh issue comment 9 --repo vitalharmony/hrse --body "{PASS_BODY}"'
        result = self._decide(cmd, check=ok_check)
        self.assertFalse(_is_denied(result))

    def test_ac3_a_fail_report_is_never_denied_even_if_check_would_refuse(self):
        """AC3: FAIL/BLOCKED are never gated. `check_gate_result` itself
        already returns True for a non-PASS verdict; this test pins that
        `decision()` doesn't add a second layer of refusal on top."""
        cmd = f'gh issue comment 9 --repo vitalharmony/hrse --body "{FAIL_BODY}"'
        result = self._decide(cmd, check=ok_check)
        self.assertFalse(_is_denied(result))

    def test_a_non_gate_body_never_calls_check_at_all(self):
        """AC2's flip side: the check must not fire on ordinary comments,
        proven by a check stub that raises if called."""
        def boom(repo, body):
            raise AssertionError("check_gate_result must not be called")

        cmd = f'gh issue comment 9 --repo vitalharmony/hrse --body "{NON_GATE_BODY}"'
        result = self._decide(cmd, check=boom)
        self.assertFalse(_is_denied(result))

    def test_no_repo_skips_the_check_rather_than_denying(self):
        """An unresolved repo means this check cannot run -- fails toward
        allow, matching #504's own posture for an unreadable input."""
        cmd = f'gh issue comment 9 --body "{PASS_BODY}"'
        result = self._decide(cmd, check=refuse_check)
        self.assertFalse(_is_denied(result))

    def test_an_unrelated_command_is_allowed(self):
        result = self._decide("git status", check=refuse_check)
        self.assertFalse(_is_denied(result))

    def test_a_non_string_command_is_denied(self):
        result = m.decision(None, self.cwd, check=refuse_check)
        self.assertTrue(_is_denied(result))

    def test_post_lane_discussion_route_is_allowed_through_untouched(self):
        """This hook does not double-check the already-guarded route --
        proven the same way as the non-gate-body case, with a check stub
        that raises if reached."""
        def boom(repo, body):
            raise AssertionError("must not re-check the already-guarded route")

        body_file = self.cwd / "body.md"
        body_file.write_text(PASS_BODY, encoding="utf-8")
        cmd = (f"python3 scripts/post_lane_discussion.py --repo vitalharmony/hrse "
               f"--issue 9 --file {body_file}")
        result = self._decide(cmd, check=boom)
        self.assertFalse(_is_denied(result))

    def test_reaches_check_via_the_real_gate_ci_looks_like_a_gate_report(self):
        """Integration point: `decision()` must actually call through to
        `gate_ci.looks_like_a_gate_report`, not a private reimplementation
        (AC2)."""
        import gate_ci
        self.assertTrue(gate_ci.looks_like_a_gate_report(PASS_BODY))
        self.assertFalse(gate_ci.looks_like_a_gate_report(NON_GATE_BODY))

    def test_default_check_is_gate_ci_check_gate_result(self):
        """The un-injected default must be the real function, not a stub
        that happens to look right in tests. Patches `gate_ci.check_gate_result`
        itself and calls `decision()` with no `check=` override -- if the
        default fell back to anything else, the patch would never fire and
        the (otherwise-refusing) real PASS body would pass through."""
        import unittest.mock as mock
        import gate_ci

        with mock.patch.object(gate_ci, "check_gate_result",
                              return_value=(False, "[GATE] REFUSED: stubbed")):
            cmd = f'gh issue comment 9 --repo vitalharmony/hrse --body "{PASS_BODY}"'
            result = self._decide(cmd)  # no check= override
        self.assertTrue(_is_denied(result))


if __name__ == "__main__":
    unittest.main()
