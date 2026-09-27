#!/usr/bin/env python3
"""Unit tests for enforce_gate_ci_on_raw_post.py (harmonic-forge#565).

`check` is injected everywhere so no test ever reaches `gh api` — mirroring
`gate_ci.py`'s own `run=` injection pattern.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


def ok_round(repo, issue, body):
    return True, "authorized"


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
        self.assertEqual(m.find_body_and_repo(args, self.cwd), (PASS_BODY, None))

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
        # Round approval has its own test class below; stubbed here so these
        # CI-check cases never reach `gh api`.
        return m.decision(command, self.cwd, check=check, round_check=ok_round)

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

    def test_a_pass_with_no_repo_is_denied(self):
        """harmonic-forge#792 preclose finding: skipping let it past both checks."""
        cmd = f'gh issue comment 9 --body "{PASS_BODY}"'
        result = self._decide(cmd, check=ok_check)
        self.assertTrue(_is_denied(result))
        self.assertIn("--repo", result["systemMessage"])

    def test_a_fail_with_no_repo_still_posts(self):
        cmd = f'gh issue comment 9 --body "{FAIL_BODY}"'
        self.assertFalse(_is_denied(self._decide(cmd, check=refuse_check)))

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


SHA = "a" * 40
NEW_SHA = "b" * 40
ROUND_PASS = f"## Lane 3 Gate Results — H999\n\n**Verdict:** PASS\n**Head-SHA:** {SHA}\n"
ROUND_FAIL = ROUND_PASS.replace("PASS", "FAIL")


def _c(comment_id: int, kind: str, sha: str = SHA, extra: str = "") -> dict:
    prefix = "Write tier W throughout.\n\n" if kind == "sweep" else ""
    return {
        "id": comment_id,
        "body": f"{prefix}{extra}body\n\n<!-- l1-post v1; kind={kind}; sha={sha} -->",
        "html_url": f"https://github.com/vitalharmony/hrse/issues/999#issuecomment-{comment_id}",
    }


#: A round approved at SHA: spec, then AE, then sweep.
APPROVED = [_c(1, "handoff"), _c(2, "spec"), _c(3, "ae"), _c(4, "sweep")]
#: hrse#2101's shape: round 1 approved at NEW_SHA's predecessor, then a round-2
#: handoff and spec with no AE, then a ready-for-l3 naming SHA.
UNAPPROVED = [_c(1, "spec", NEW_SHA), _c(2, "ae", NEW_SHA), _c(3, "sweep", NEW_SHA),
              _c(4, "handoff", SHA), _c(5, "spec", SHA), _c(6, "ready-for-l3", SHA)]


class RoundApprovalTests(unittest.TestCase):
    """harmonic-forge#792 AC2/AC4: a raw `gh issue comment` PASS for a round
    nobody approved is refused, through the real `resolve_gate_authority`.
    Only the comments fetch is stubbed."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmpdir.name)
        self.body = self.cwd / "gate.md"

    def tearDown(self):
        self.tmpdir.cleanup()

    def _decide(self, body: str, comments: list[dict], command: str | None = None) -> tuple[dict, list]:
        self.body.write_text(body, encoding="utf-8")
        command = command or f"gh issue comment 999 --repo vitalharmony/hrse --body-file {self.body}"
        calls = []

        def fetch(repo, issue):
            calls.append((repo, issue))
            return comments

        with mock.patch.object(m.check_lane3_ready, "fetch_comments", side_effect=fetch):
            return m.decision(command, self.cwd, check=ok_check), calls

    def test_an_unapproved_round_pass_is_denied(self):
        result, calls = self._decide(ROUND_PASS, UNAPPROVED)
        self.assertTrue(_is_denied(result))
        self.assertIn("unapproved round", result["systemMessage"])
        self.assertEqual(calls, [("vitalharmony/hrse", 999)])

    def test_an_approved_round_pass_is_allowed(self):
        result, _ = self._decide(ROUND_PASS, APPROVED)
        self.assertFalse(_is_denied(result))

    def test_an_abbreviated_head_sha_still_matches_the_full_footer(self):
        """Preclose finding: real reports state 8-char SHAs; the footer is full."""
        result, _ = self._decide(ROUND_PASS.replace(SHA, SHA[:8]), APPROVED)
        self.assertFalse(_is_denied(result))

    def test_a_fail_is_never_checked(self):
        result, calls = self._decide(ROUND_FAIL, UNAPPROVED)
        self.assertFalse(_is_denied(result))
        self.assertEqual(calls, [])

    def test_the_same_refusal_through_post_comment_py(self):
        command = (f"python3 tools/gh/post_comment.py --repo vitalharmony/hrse "
                   f"--issue 999 --file {self.body}")
        result, calls = self._decide(ROUND_PASS, UNAPPROVED, command)
        self.assertTrue(_is_denied(result))
        self.assertEqual(calls, [("vitalharmony/hrse", 999)])

    def test_the_same_refusal_through_gh_as_with_short_flags(self):
        command = f"gh-as vitalharmony gh issue comment 999 -R vitalharmony/hrse -F {self.body}"
        result, _ = self._decide(ROUND_PASS, UNAPPROVED, command)
        self.assertTrue(_is_denied(result))

    def test_a_pass_with_no_head_sha_is_denied(self):
        body = "## Lane 3 Gate Results — H999\n\n**Verdict:** PASS\n"
        ok, message = m.round_approval("vitalharmony/hrse", 999, body)
        self.assertFalse(ok)
        self.assertIn("head SHA", message)

    def test_an_unresolved_issue_is_denied(self):
        ok, message = m.round_approval("vitalharmony/hrse", None, ROUND_PASS)
        self.assertFalse(ok)

    def test_a_missing_gh_binary_is_denied(self):
        """Preclose finding: FileNotFoundError is not SystemExit."""
        with mock.patch.object(m.check_lane3_ready, "fetch_comments",
                               side_effect=FileNotFoundError("gh")):
            ok, _ = m.round_approval("vitalharmony/hrse", 999, ROUND_PASS)
        self.assertFalse(ok)

    def test_a_failed_fetch_is_denied(self):
        with mock.patch.object(m.check_lane3_ready, "fetch_comments", side_effect=SystemExit(1)):
            ok, message = m.round_approval("vitalharmony/hrse", 999, ROUND_PASS)
        self.assertFalse(ok)
        self.assertIn("cannot fetch", message)


class FindIssueTests(unittest.TestCase):
    def test_gh_positional_number(self):
        self.assertEqual(m.find_issue(["gh", "issue", "comment", "42", "--repo", "o/r"]), 42)

    def test_gh_positional_after_value_flags(self):
        args = ["gh", "issue", "comment", "--repo", "o/r", "--body-file", "x.md", "42"]
        self.assertEqual(m.find_issue(args), 42)

    def test_gh_positional_url(self):
        args = ["gh", "issue", "comment", "https://github.com/o/r/issues/42", "-F", "x.md"]
        self.assertEqual(m.find_issue(args), 42)

    def test_gh_as_wrapped(self):
        args = ["gh-as", "acct", "gh", "issue", "comment", "42", "-F", "x.md"]
        self.assertEqual(m.find_issue(args), 42)

    def test_post_comment_issue_flag(self):
        args = ["python3", "post_comment.py", "--repo", "o/r", "--issue", "42", "--file", "x"]
        self.assertEqual(m.find_issue(args), 42)

    def test_mise_post_comment_issue_equals(self):
        args = ["mise", "run", "post-comment", "--", "--repo", "o/r", "--issue=42", "--file", "x"]
        self.assertEqual(m.find_issue(args), 42)


if __name__ == "__main__":
    unittest.main()
