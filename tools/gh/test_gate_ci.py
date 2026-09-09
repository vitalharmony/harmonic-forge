#!/usr/bin/env python3
"""Tests for gate_ci.py (harmonic-forge#504).

AC1 says a prose instruction does not satisfy this issue, so the check is
mechanical — and a mechanical check is only worth what its tests are. The one
that matters most is `test_replaying_the_live_incident_does_not_pass`: the
issue's AC5 names a real SHA whose CI is still red on GitHub today, and that
test drives the real code path against a recorded copy of that real payload.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gate_ci  # noqa: E402


def fake_runs(*runs):
    """A `run` stand-in returning these check-run dicts."""
    def run(cmd):
        return 0, json.dumps(list(runs))
    return run


def failing_run(code=1, out="gh: not found"):
    def run(cmd):
        return code, out
    return run


PASS_BODY = """## Lane 3 Gate Results — harmonic-forge#494

**Verdict:** PASS
**Head-SHA:** 0359854f1234567890abcdef1234567890abcdef

All nine cases executed.
"""


class VerdictParsingTests(unittest.TestCase):
    def test_a_verdict_line_is_read(self):
        self.assertEqual(gate_ci.verdict_of(PASS_BODY), "PASS")

    def test_fail_and_blocked_are_read(self):
        for word in ("FAIL", "BLOCKED"):
            with self.subTest(word=word):
                self.assertEqual(
                    gate_ci.verdict_of(f"**Verdict:** {word}\n"), word)

    def test_a_verdict_in_the_heading_is_read(self):
        """`post_lane_discussion` deliberately does not require PASS/FAIL in the
        heading, so both shapes exist in the corpus."""
        self.assertEqual(
            gate_ci.verdict_of("### Lane 3 Gate Results — H395 — PASS\n"), "PASS")

    def test_no_verdict_reads_as_none(self):
        self.assertIsNone(gate_ci.verdict_of("## Lane 3 Gate Results\n\nnotes\n"))

    def test_the_sha_is_read(self):
        self.assertTrue(gate_ci.gated_sha(PASS_BODY).startswith("0359854f"))

    def test_an_abbreviated_sha_is_read(self):
        self.assertEqual(gate_ci.gated_sha("Head-SHA: `0359854`\n"), "0359854")

    def test_a_body_with_no_sha_reads_as_none(self):
        self.assertIsNone(gate_ci.gated_sha("**Verdict:** PASS\n"))


class OnlyPassIsGatedTests(unittest.TestCase):
    """A verification layer that can silence a failure report is worse than
    none. Only PASS authorises a merge, so only PASS has to clear the bar."""

    def test_fail_posts_without_touching_ci(self):
        def explode(cmd):
            raise AssertionError("CI must not be consulted for a FAIL")
        ok, _ = gate_ci.check_gate_result("o/r", "**Verdict:** FAIL\n", run=explode)
        self.assertTrue(ok)

    def test_blocked_posts_without_touching_ci(self):
        def explode(cmd):
            raise AssertionError("CI must not be consulted for a BLOCKED")
        ok, _ = gate_ci.check_gate_result("o/r", "**Verdict:** BLOCKED\n", run=explode)
        self.assertTrue(ok)

    def test_a_body_with_no_verdict_is_not_gated(self):
        ok, _ = gate_ci.check_gate_result("o/r", "no verdict here\n",
                                          run=failing_run())
        self.assertTrue(ok)


class PassRequiresGreenCiTests(unittest.TestCase):
    def test_green_ci_lets_a_pass_through(self):
        ok, msg = gate_ci.check_gate_result(
            "o/r", PASS_BODY,
            run=fake_runs({"name": "verify", "status": "completed",
                           "conclusion": "success"}))
        self.assertTrue(ok, msg)

    def test_red_ci_refuses_a_pass(self):
        """AC1, and the incident itself."""
        ok, msg = gate_ci.check_gate_result(
            "o/r", PASS_BODY,
            run=fake_runs({"name": "verify", "status": "completed",
                           "conclusion": "failure"}))
        self.assertFalse(ok)
        self.assertIn("RED", msg)
        self.assertIn("verify:failure", msg)

    def test_a_pending_check_refuses_a_pass(self):
        """AC3: waiting is the correct behaviour, not a judgment call."""
        ok, msg = gate_ci.check_gate_result(
            "o/r", PASS_BODY,
            run=fake_runs({"name": "verify", "status": "in_progress",
                           "conclusion": None}))
        self.assertFalse(ok)
        self.assertIn("BLOCKED", msg)

    def test_no_checks_at_all_refuses_a_pass(self):
        """'No checks yet' is not 'nothing failing'.

        I made exactly this mistake in a wait-loop while implementing this
        issue: it tested "every check completed", an empty list satisfied that
        vacuously, and it reported done on a PR whose CI had not registered.
        """
        ok, msg = gate_ci.check_gate_result("o/r", PASS_BODY, run=fake_runs())
        self.assertFalse(ok)
        self.assertIn("not completed", msg)

    def test_an_unreadable_api_refuses_a_pass(self):
        """Fail closed. An unreachable API is not evidence of health."""
        ok, msg = gate_ci.check_gate_result("o/r", PASS_BODY, run=failing_run())
        self.assertFalse(ok)

    def test_a_pass_with_no_sha_is_refused(self):
        """AC2. Without the SHA there is nothing to check, and a reader cannot
        tell what was gated."""
        ok, msg = gate_ci.check_gate_result(
            "o/r", "**Verdict:** PASS\n", run=failing_run())
        self.assertFalse(ok)
        self.assertIn("head SHA", msg)

    def test_skipped_and_neutral_are_not_failures(self):
        """A skipped job is one that correctly decided it had nothing to do —
        every hrse PR has two, and treating them as red would refuse every
        legitimate PASS in that repo."""
        ok, msg = gate_ci.check_gate_result(
            "o/r", PASS_BODY,
            run=fake_runs({"name": "verify", "status": "completed", "conclusion": "success"},
                          {"name": "build-and-push", "status": "completed", "conclusion": "skipped"},
                          {"name": "lint", "status": "completed", "conclusion": "neutral"}))
        self.assertTrue(ok, msg)

    def test_one_red_among_many_greens_still_refuses(self):
        ok, _ = gate_ci.check_gate_result(
            "o/r", PASS_BODY,
            run=fake_runs({"name": "a", "status": "completed", "conclusion": "success"},
                          {"name": "b", "status": "completed", "conclusion": "failure"},
                          {"name": "c", "status": "completed", "conclusion": "success"}))
        self.assertFalse(ok)

    def test_an_unrecognised_conclusion_refuses(self):
        """Fail closed on a vocabulary this module does not know, rather than
        guessing it is benign."""
        ok, msg = gate_ci.check_gate_result(
            "o/r", PASS_BODY,
            run=fake_runs({"name": "verify", "status": "completed",
                           "conclusion": "something_new"}))
        self.assertFalse(ok)


class Ac5LiveIncidentReplayTests(unittest.TestCase):
    """AC5: replaying the gate on harmonic-forge#494's merge SHA must not PASS.

    `0359854` is the real merge commit of PR #501 (which closed #494). Its
    `verify` check is still `failure` on GitHub today — confirmed live while
    writing this. The payload below is that real response, recorded so the test
    is deterministic and offline; the SHA and conclusion are not invented.
    """

    RECORDED = {"name": "verify", "status": "completed", "conclusion": "failure"}

    def test_replaying_the_live_incident_does_not_pass(self):
        body = ("## Lane 3 Gate Results — harmonic-forge#494\n\n"
                "**Verdict:** PASS\n**Head-SHA:** 0359854\n")
        ok, msg = gate_ci.check_gate_result(
            "vitalharmony/harmonic-forge", body, run=fake_runs(self.RECORDED))
        self.assertFalse(ok, "the incident's own PASS must not be postable")
        self.assertIn("0359854", msg)
        self.assertIn("verify:failure", msg)

    def test_the_same_report_as_fail_is_postable(self):
        """The corrective verdict must never be blocked."""
        body = ("## Lane 3 Gate Results — harmonic-forge#494\n\n"
                "**Verdict:** FAIL\n**Head-SHA:** 0359854\n")
        ok, _ = gate_ci.check_gate_result(
            "vitalharmony/harmonic-forge", body, run=fake_runs(self.RECORDED))
        self.assertTrue(ok)


class CiConclusionStatesTests(unittest.TestCase):
    def test_absent_is_its_own_state_not_green(self):
        state, _ = gate_ci.ci_conclusion("o/r", "abc1234", run=fake_runs())
        self.assertEqual(state, "absent")

    def test_unparseable_payload_is_unknown(self):
        state, _ = gate_ci.ci_conclusion("o/r", "abc1234",
                                         run=lambda cmd: (0, "{not json"))
        self.assertEqual(state, "unknown")

    def test_pending_wins_over_a_red_sibling(self):
        """Report the weaker fact. 'Still running' tells the operator to wait;
        'red' tells them to fix — and while anything is pending, which one is
        true is not yet known."""
        state, _ = gate_ci.ci_conclusion(
            "o/r", "abc1234",
            run=fake_runs({"name": "a", "status": "completed", "conclusion": "failure"},
                          {"name": "b", "status": "queued", "conclusion": None}))
        self.assertEqual(state, "pending")


class CliTests(unittest.TestCase):
    def test_the_cli_exit_code_distinguishes_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.md"
            path.write_text("**Verdict:** FAIL\n", encoding="utf-8")
            self.assertEqual(
                gate_ci.main(["--repo", "o/r", "--file", str(path)]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
