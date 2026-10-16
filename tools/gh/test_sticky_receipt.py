#!/usr/bin/env python3
"""Tests for the sticky-wicket receipt (harmonic-forge#997)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import l1_post  # noqa: E402
import sticky_receipt as sr  # noqa: E402
import watch_lane_posts as belt  # noqa: E402

REPO, ISSUE = "vitalharmony/openclaw-projects", 35


def gate(cid, verdict, sha="abc1234"):
    at = f" at `{sha}`" if sha else ""
    body = (f"## Lane 3 Gate Results — O35\n\n**Verdict:** {verdict}{at}, Tier R.\n"
            "**Finding:** x\n**Next:** y\n\n" + (f"Head-SHA: {sha}\n\n" if sha else "")
            + "<!-- l1-post v1; kind=gate-result; posted-by=LANE3 -->")
    return {"id": cid, "body": body}


def note(cid, text="discussion"):
    return {"id": cid, "body": text}


class ReceiptHome(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(sr, "receipt_dir", return_value=Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def write_receipt(self, verdict="PATCH", through=0, repo=REPO, issue=ISSUE):
        sr.receipt_path(REPO, ISSUE).write_text(json.dumps(
            {"repo": repo, "issue": issue, "verdict": verdict, "through": through}))


class OwedTests(ReceiptHome):
    def test_two_consecutive_fails_are_owed(self):
        self.assertEqual(sr.owed([gate(1, "FAIL (`L3F`)"), note(2), gate(3, "FAIL (`L3F`)")]), (1, 3))

    def test_blocked_counts_as_non_pass(self):
        """O35's two blocked rounds never fired the old FAIL-only trigger."""
        self.assertEqual(sr.owed([gate(1, "BLOCKED (`L3B`)"), gate(2, "BLOCKED (`L3B`)")]), (1, 2))

    def test_an_unreadable_verdict_fails_closed(self):
        self.assertEqual(sr.owed([gate(1, "FAIL"), gate(2, "who knows")]), (1, 2))

    def test_a_pass_between_or_last_is_not_owed(self):
        self.assertIsNone(sr.owed([gate(1, "FAIL"), gate(2, "PASS (`L3P`)")]))
        self.assertIsNone(sr.owed([gate(1, "PASS"), gate(2, "FAIL")]))

    def test_one_result_is_not_owed(self):
        self.assertIsNone(sr.owed([gate(1, "FAIL"), note(2)]))

    def test_a_receipt_through_the_newer_result_covers_it(self):
        comments = [gate(1, "FAIL"), gate(5, "FAIL")]
        self.assertIsNotNone(sr.refusal(REPO, ISSUE, comments))
        self.write_receipt(through=5)
        self.assertIsNone(sr.refusal(REPO, ISSUE, comments))

    def test_a_receipt_through_an_older_result_does_not(self):
        self.write_receipt(through=1)
        self.assertIsNotNone(sr.refusal(REPO, ISSUE, [gate(1, "FAIL"), gate(5, "FAIL")]))

    def test_a_receipt_for_another_issue_does_not(self):
        self.write_receipt(through=5, issue=ISSUE + 1)
        self.assertIsNotNone(sr.refusal(REPO, ISSUE, [gate(1, "FAIL"), gate(5, "FAIL")]))

    def test_the_refusal_names_both_results_and_the_command(self):
        reason = sr.refusal(REPO, ISSUE, [gate(11, "FAIL"), gate(22, "BLOCKED")])
        self.assertIn("issuecomment-11", reason)
        self.assertIn("issuecomment-22", reason)
        self.assertIn("sticky_receipt.py record", reason)
        self.assertIn("--through 22", reason)


class RecordTests(ReceiptHome):
    report_body = "## Sticky-wicket verdict: PATCH\n\nnarrow fix"
    def _gh(self, comments, comment_issue=ISSUE):
        def fake(path):
            if path.endswith("/comments"):
                return comments
            return {"id": 99, "body": self.report_body,
                    "issue_url": f"https://api.github.com/repos/{REPO}/issues/{comment_issue}",
                    "html_url": f"https://github.com/{REPO}/issues/{ISSUE}#issuecomment-99"}
        return mock.patch.object(sr, "_gh_json", side_effect=fake)

    def test_record_requires_through_to_be_a_gate_result(self):
        with self._gh([gate(1, "FAIL"), note(2)]), self.assertRaises(SystemExit):
            sr.record(REPO, ISSUE, "PATCH", 2, "https://github.com/x#issuecomment-99")

    def test_record_requires_the_report_on_the_same_issue(self):
        with self._gh([gate(1, "FAIL")], comment_issue=ISSUE + 1), self.assertRaises(SystemExit):
            sr.record(REPO, ISSUE, "PATCH", 1, "https://github.com/x#issuecomment-99")

    def test_record_refuses_a_report_that_is_not_a_sticky_wicket_verdict(self):
        self.report_body = "## Discussion\n\nnothing about the approach"
        with self._gh([gate(1, "FAIL"), gate(3, "FAIL")]), self.assertRaises(SystemExit):
            sr.record(REPO, ISSUE, "PATCH", 3, "https://github.com/x#issuecomment-99")

    def test_record_refuses_a_report_older_than_the_gate_result(self):
        with self._gh([gate(1, "FAIL"), gate(150, "FAIL")]), self.assertRaises(SystemExit):
            sr.record(REPO, ISSUE, "PATCH", 150, "https://github.com/x#issuecomment-99")

    def test_record_writes_a_receipt_that_covers(self):
        with self._gh([gate(1, "FAIL"), gate(3, "FAIL")]):
            sr.record(REPO, ISSUE, "PATCH", 3, "https://github.com/x#issuecomment-99")
        self.assertTrue(sr.covered(REPO, ISSUE, 3))


class ReworkRefusalTests(ReceiptHome):
    """The call site: `l1_post --kind rework` reads the thread and refuses."""

    def _run(self, comments, rc=0):
        result = mock.Mock(returncode=rc, stdout=json.dumps(comments), stderr="")
        return mock.patch.object(l1_post, "run", return_value=result)

    def test_rework_after_two_fails_is_refused(self):
        with self._run([gate(1, "FAIL"), gate(2, "FAIL")]), self.assertRaises(SystemExit):
            l1_post.validate_sticky_wicket(REPO, ISSUE)

    def test_rework_after_a_pass_posts(self):
        with self._run([gate(1, "FAIL"), gate(2, "PASS")]):
            l1_post.validate_sticky_wicket(REPO, ISSUE)

    def test_an_unreadable_thread_refuses(self):
        with self._run([], rc=1), self.assertRaises(SystemExit):
            l1_post.validate_sticky_wicket(REPO, ISSUE)

    def test_every_kind_that_hands_work_forward_is_checked(self):
        self.assertEqual(set(l1_post.STICKY_CHECKED_KINDS),
                         {"rework", "handoff", "ready-for-l3", "ae", "sweep", "ae-and-sweep"})


class MainCallSiteTests(ReceiptHome):
    """Sticky-wicket PATCH: drive `l1_post.main()` itself, so a check removed from
    the call site turns these red."""

    def _main(self, kind, comments):
        argv = ["l1_post.py", "--repo", REPO, "--issue", str(ISSUE), "--kind", kind,
                "--sha", "deadbeef", "--branch", "feat/35-x", "--file", "/dev/null"]
        result = mock.Mock(returncode=0, stdout=json.dumps(comments), stderr="")
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(l1_post, "resolve_sha", return_value="deadbeef"), \
                mock.patch.object(l1_post, "regular_body", return_value="body"), \
                mock.patch.object(l1_post, "reject_reserved_marker"), \
                mock.patch.object(l1_post, "validate_lead"), \
                mock.patch.object(l1_post, "run", return_value=result), \
                mock.patch.object(l1_post, "post_kind",
                                  return_value=("https://example/1", 1)) as post, \
                mock.patch.object(l1_post, "record_queue_candidate"):
            try:
                l1_post.main()
            except SystemExit:
                return post, True
        return post, False

    def test_rework_and_ready_for_l3_are_refused_before_posting(self):
        for kind in ("rework", "ready-for-l3"):
            with self.subTest(kind=kind):
                post, refused = self._main(kind, [gate(1, "FAIL"), gate(2, "BLOCKED")])
                self.assertTrue(refused)
                post.assert_not_called()

    def test_a_covering_receipt_lets_the_post_through(self):
        self.write_receipt(through=2)
        post, refused = self._main("rework", [gate(1, "FAIL"), gate(2, "BLOCKED")])
        self.assertFalse(refused)
        post.assert_called_once()

    def test_the_late_recheck_sits_immediately_before_the_post(self):
        """The world checks inside `_post_kind` are long; a result landing meanwhile
        must be seen, so the check is repeated right before `comment_body`."""
        source = Path(l1_post.__file__).read_text()
        late = source.index("        validate_sticky_wicket(repo, issue, kind=kind, sha=sha)\n")
        self.assertLess(late, source.index("    url, comment_id = comment_body(repo, issue"))
        self.assertGreater(late, source.index("def _post_kind("))


class BeltEventTests(ReceiptHome):
    def _queue(self, comments):
        with mock.patch.object(belt, "_fetch_all_comments", return_value=comments), \
                mock.patch.object(belt, "_issue_meta", return_value=("open", set(), None)), \
                mock.patch.object(belt, "_active_milestones", return_value=None):
            return belt.discover_queue(REPO, "l1", {ISSUE})[0]

    def test_discover_queue_emits_the_event_for_lane_1(self):
        """The call site, not just the helper."""
        self.assertEqual(self._queue([gate(1, "FAIL"), gate(2, "FAIL")]),
                         {ISSUE: "gate-result owes=sticky-wicket"})

    def test_an_owed_plan_review_is_not_hidden_by_the_trigger(self):
        plan = {"id": 3, "body": "## Plan\n\n<!-- l1-post v1; kind=plan; posted-by=LANE2 -->"}
        self.assertEqual(self._queue([gate(1, "FAIL"), gate(2, "FAIL"), plan]),
                         {ISSUE: "plan owes=plan-review"})

    def test_a_covered_trigger_emits_nothing(self):
        self.write_receipt(through=2)
        self.assertEqual(self._queue([gate(1, "FAIL"), gate(2, "FAIL")]), {})

    def test_owed_and_uncovered_is_a_lane_1_event(self):
        comments = [gate(1, "FAIL"), gate(2, "FAIL")]
        self.assertTrue(belt._sticky_wicket_owed(REPO, ISSUE, comments))
        self.write_receipt(through=2)
        self.assertFalse(belt._sticky_wicket_owed(REPO, ISSUE, comments))

    def test_the_event_owes_sticky_wicket(self):
        self.assertEqual(belt.OWES["l1"]["gate-result"], "sticky-wicket")



class SameShaRetryTests(ReceiptHome):
    """harmonic-forge#1019: a BLOCKED pair at one SHA exempts an AE/sweep on that SHA."""

    def blocked(self, cid, sha="3f4747e2742fa83f"):
        return gate(cid, "BLOCKED (`L3B`)", sha)

    def test_ae_same_sha_retry_posts(self):
        comments = [self.blocked(1), self.blocked(2)]
        for kind in ("ae", "sweep", "ae-and-sweep"):
            with self.subTest(kind=kind):
                self.assertIsNone(sr.refusal(REPO, ISSUE, comments, kind=kind,
                                             attested_sha="3f4747e2742fa83f82645561584060a4501f60bc"))

    def test_third_same_sha_block_refused(self):
        comments = [self.blocked(1), self.blocked(2), self.blocked(3)]
        self.assertIsNotNone(sr.refusal(REPO, ISSUE, comments, kind="ae",
                                        attested_sha="3f4747e2742fa83f"))

    def test_ae_different_sha_refused(self):
        comments = [self.blocked(1), self.blocked(2)]
        self.assertIsNotNone(sr.refusal(REPO, ISSUE, comments, kind="ae", attested_sha="deadbeef"))

    def test_blocked_pair_at_different_shas_refused(self):
        comments = [self.blocked(1, "aaaa111"), self.blocked(2, "bbbb222")]
        self.assertIsNotNone(sr.refusal(REPO, ISSUE, comments, kind="ae", attested_sha="bbbb222"))

    def test_missing_sha_pair_refused(self):
        comments = [gate(1, "BLOCKED (`L3B`)", sha=None), self.blocked(2)]
        self.assertIsNotNone(sr.refusal(REPO, ISSUE, comments, kind="ae",
                                        attested_sha="3f4747e2742fa83f"))

    def test_rework_after_blocked_pair_refused(self):
        comments = [self.blocked(1), self.blocked(2)]
        for kind in ("rework", "handoff", "ready-for-l3"):
            with self.subTest(kind=kind):
                self.assertIsNotNone(sr.refusal(REPO, ISSUE, comments, kind=kind,
                                                attested_sha="3f4747e2742fa83f"))

    def test_fail_blocked_pair_refused(self):
        comments = [gate(1, "FAIL (`L3F`)", "3f4747e2742fa83f"), self.blocked(2)]
        self.assertIsNotNone(sr.refusal(REPO, ISSUE, comments, kind="ae",
                                        attested_sha="3f4747e2742fa83f"))

    def test_the_call_site_passes_kind_and_sha(self):
        result = mock.Mock(returncode=0, stdout=json.dumps([self.blocked(1), self.blocked(2)]), stderr="")
        with mock.patch.object(l1_post, "run", return_value=result):
            l1_post.validate_sticky_wicket(REPO, ISSUE, kind="ae", sha="3f4747e2742fa83f")
            with self.assertRaises(SystemExit):
                l1_post.validate_sticky_wicket(REPO, ISSUE, kind="rework", sha="3f4747e2742fa83f")

    def test_belt_silent_for_same_sha_retry(self):
        self.assertFalse(belt._sticky_wicket_owed(REPO, ISSUE, [self.blocked(1), self.blocked(2)]))

    def test_belt_emits_for_a_fail_pair(self):
        self.assertTrue(belt._sticky_wicket_owed(REPO, ISSUE, [gate(1, "FAIL"), gate(2, "FAIL")]))

    def test_belt_emits_for_a_third_same_sha_block(self):
        self.assertTrue(belt._sticky_wicket_owed(
            REPO, ISSUE, [self.blocked(1), self.blocked(2), self.blocked(3)]))

    def test_third_block_with_unreadable_sha_refused(self):
        comments = [gate(1, "BLOCKED (`L3B`)", sha=None), self.blocked(2), self.blocked(3)]
        self.assertIsNotNone(sr.refusal(REPO, ISSUE, comments, kind="ae",
                                        attested_sha="3f4747e2742fa83f"))
        self.assertTrue(belt._sticky_wicket_owed(REPO, ISSUE, comments))

    def test_a_short_sha_cannot_bridge_two_full_shas(self):
        comments = [self.blocked(1, "abcdef0" + "1" * 33), self.blocked(2, "abcdef0")]
        self.assertIsNotNone(sr.refusal(REPO, ISSUE, comments, kind="ae",
                                        attested_sha="abcdef0" + "2" * 33))
        self.assertIsNone(sr.refusal(REPO, ISSUE, comments, kind="ae",
                                     attested_sha="abcdef0" + "1" * 33))


    def test_undecidable_third_newest_refused(self):
        for third in (gate(1, "who knows", "3f4747e2742fa83f"),
                      gate(1, "BLOCKED (`L3B`)", "3F4747E2742FA83F"),
                      gate(1, "BLOCKED (`L3B`)", "3f47")):
            with self.subTest(third=third["body"][:60]):
                comments = [third, self.blocked(2), self.blocked(3)]
                self.assertIsNotNone(sr.refusal(REPO, ISSUE, comments, kind="ae",
                                                attested_sha="3f4747e2742fa83f"))
                self.assertTrue(belt._sticky_wicket_owed(REPO, ISSUE, comments))

    def test_decidably_other_third_newest_stays_exempt(self):
        for third in (gate(1, "PASS (`L3P`)", "3f4747e2742fa83f"), gate(1, "FAIL (`L3F`)", "3f4747e2742fa83f"),
                      gate(1, "BLOCKED (`L3B`)", "aaaa1111")):
            with self.subTest(third=third["body"][:60]):
                comments = [third, self.blocked(2), self.blocked(3)]
                self.assertIsNone(sr.refusal(REPO, ISSUE, comments, kind="ae",
                                             attested_sha="3f4747e2742fa83f"))

    def test_uppercase_attested_sha_matches(self):
        self.assertIsNone(sr.refusal(REPO, ISSUE, [self.blocked(1), self.blocked(2)], kind="ae",
                                     attested_sha="3F4747E2742FA83F"))


    def test_an_uppercase_pair_is_read_as_the_same_sha(self):
        comments = [self.blocked(1, "3F4747E2742FA83F"), self.blocked(2, "3F4747E2742FA83F")]
        self.assertIsNone(sr.refusal(REPO, ISSUE, comments, kind="ae",
                                     attested_sha="3f4747e2742fa83f"))


if __name__ == "__main__":
    unittest.main()
