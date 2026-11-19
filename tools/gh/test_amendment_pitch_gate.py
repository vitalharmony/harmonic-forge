#!/usr/bin/env python3
"""harmonic-forge#1426: a design-changing Lane 1 amendment to a tooling-exception
issue with a posted handoff needs a fresh pitch verdict, on every amendment route;
and the preclose agent hunts the readers of every contract a diff changes."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import pitch_receipt  # noqa: E402
import l1_post  # noqa: E402
import post_lane_discussion  # noqa: E402
import post_lane1_issue  # noqa: E402
import post_comment  # noqa: E402

REPO, ISSUE = "vitalharmony/harmonic-forge", 91426
TE = {"tooling-exception"}
HANDOFF = [{"body": "## Handoff\n\n<!-- l1-post v1; kind=handoff; sha=abc; -->"}]
NO_HANDOFF = [{"body": "just a note\n<!-- l1-post v1; kind=discussion; posted-by=LANE1 -->"}]


class _Receipts(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = patch.object(pitch_receipt, "receipt_dir", return_value=Path(tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def spend(self) -> None:
        pitch_receipt.record(REPO, ISSUE, "PROCEED")
        pitch_receipt.consume(REPO, ISSUE, "handoff-url")

    def fresh(self) -> None:
        pitch_receipt.record(REPO, ISSUE, "PROCEED")


class HelperTests(_Receipts):
    def test_gate_needs_label_and_posted_handoff(self) -> None:
        self.assertTrue(pitch_receipt.amendment_gate_applies(TE, HANDOFF))
        self.assertFalse(pitch_receipt.amendment_gate_applies(TE, NO_HANDOFF))
        self.assertFalse(pitch_receipt.amendment_gate_applies({"bug"}, HANDOFF))

    def test_undeclared_amendment_refused(self) -> None:
        reason = pitch_receipt.amendment_refusal(REPO, ISSUE, TE, HANDOFF, None)
        self.assertIn("--amendment design", reason)

    def test_design_with_spent_receipt_refused_naming_record(self) -> None:
        self.spend()
        reason = pitch_receipt.amendment_refusal(REPO, ISSUE, TE, HANDOFF, "design")
        self.assertIn("pitch_receipt.py record", reason)
        self.assertIn("design-changing amendment", reason)

    def test_design_with_fresh_receipt_allowed(self) -> None:
        self.fresh()
        self.assertIsNone(pitch_receipt.amendment_refusal(REPO, ISSUE, TE, HANDOFF, "design"))

    def test_wording_posts_with_spent_receipt(self) -> None:
        self.spend()
        self.assertIsNone(pitch_receipt.amendment_refusal(REPO, ISSUE, TE, HANDOFF, "wording"))

    def test_before_handoff_never_gated(self) -> None:
        self.assertIsNone(pitch_receipt.amendment_refusal(REPO, ISSUE, TE, NO_HANDOFF, None))


class FooterAndLaneTests(_Receipts):
    def test_a_quoted_footer_is_not_a_posted_handoff(self) -> None:
        quoted = [{"body": "example:\n```\n<!-- l1-post v1; kind=handoff; sha=abc; -->\n```\nmore text"}]
        self.assertFalse(pitch_receipt.amendment_gate_applies(TE, quoted))

    def test_lane_unset_is_gated_lanes_two_and_three_are_not(self) -> None:
        self.spend()
        for lane, gated in ((None, True), ("1", True), ("2", False), ("3", False)):
            env = {} if lane is None else {"LANE": lane}
            with patch.dict(os.environ, env, clear=False) as e:
                if lane is None:
                    e.pop("LANE", None)
                with patch.object(post_lane_discussion, "issue_labels", return_value=TE), \
                        patch.object(post_lane_discussion.check_lane3_ready, "fetch_comments",
                                     return_value=HANDOFF):
                    if gated:
                        with self.assertRaises(SystemExit):
                            post_lane_discussion.require_amendment_pitch(REPO, ISSUE, "discussion", None)
                    else:
                        self.assertFalse(post_lane_discussion.require_amendment_pitch(
                            REPO, ISSUE, "discussion", None))


class ConsumeTests(_Receipts):
    def test_correction_consumes_only_when_the_gate_applied(self) -> None:
        for comments, consumed in ((HANDOFF, True), (NO_HANDOFF, False)):
            self.fresh()
            with patch.object(l1_post, "check_lane3_ready_fetch_comments", return_value=comments):
                l1_post.validate_correction_amendment(REPO, ISSUE, TE, "design")
            self.assertEqual(bool(l1_post._CORRECTION_GATED.get((REPO, ISSUE))), consumed)

    def test_body_amendment_consumes_after_a_design_edit(self) -> None:
        self.fresh()
        with patch.dict(os.environ, {"LANE": "1"}), \
                patch.object(post_lane1_issue, "issue_labels", return_value=TE), \
                patch.object(post_lane1_issue, "issue_comments", return_value=HANDOFF):
            self.assertTrue(post_lane1_issue.require_amendment_pitch(REPO, ISSUE, "design"))

    def test_discussion_post_consumes_the_verdict(self) -> None:
        """main() spends the receipt after a gated design post (consume is called)."""
        import inspect
        source = inspect.getsource(post_lane_discussion.main)
        self.assertIn("pitch_receipt.consume(args.repo, args.issue, url)", source)
        self.assertIn("if consume_pitch:", source)


class DiscussionRouteTests(_Receipts):
    def gate(self, declared, comments=HANDOFF, labels=TE, lane="1", kind="discussion"):
        with patch.dict(os.environ, {"LANE": lane}), \
                patch.object(post_lane_discussion, "issue_labels", return_value=labels), \
                patch.object(post_lane_discussion.check_lane3_ready, "fetch_comments",
                             return_value=comments):
            return post_lane_discussion.require_amendment_pitch(REPO, ISSUE, kind, declared)

    def test_design_with_spent_receipt_refused(self) -> None:
        self.spend()
        with self.assertRaises(SystemExit):
            self.gate("design")

    def test_design_with_fresh_receipt_consumes(self) -> None:
        self.fresh()
        self.assertTrue(self.gate("design"))

    def test_wording_posts(self) -> None:
        self.spend()
        self.assertFalse(self.gate("wording"))

    def test_missing_flag_refused(self) -> None:
        with self.assertRaises(SystemExit):
            self.gate(None)

    def test_other_lanes_and_non_te_unaffected(self) -> None:
        self.spend()
        self.assertFalse(self.gate(None, lane="2"))
        self.assertFalse(self.gate(None, labels={"bug"}))
        self.assertFalse(self.gate(None, kind="plan"))


class IssueBodyRouteTests(_Receipts):
    def gate(self, declared, comments=HANDOFF, labels=TE):
        with patch.dict(os.environ, {"LANE": "1"}), \
                patch.object(post_lane1_issue, "issue_labels", return_value=labels), \
                patch.object(post_lane1_issue, "issue_comments", return_value=comments):
            return post_lane1_issue.require_amendment_pitch(REPO, ISSUE, declared)

    def test_design_with_spent_receipt_refused(self) -> None:
        self.spend()
        with self.assertRaises(SystemExit):
            self.gate("design")

    def test_body_edit_before_handoff_posts_without_flag(self) -> None:
        self.assertFalse(self.gate(None, comments=NO_HANDOFF))


class CorrectionReworkRouteTests(_Receipts):
    def gate(self, declared, comments=HANDOFF, labels=TE):
        with patch.object(l1_post, "check_lane3_ready_fetch_comments", return_value=comments):
            l1_post.validate_correction_amendment(REPO, ISSUE, labels, declared)

    def test_design_correction_with_spent_receipt_refused(self) -> None:
        self.spend()
        with self.assertRaises(SystemExit):
            self.gate("design")

    def test_wording_correction_posts(self) -> None:
        self.spend()
        self.gate("wording")

    def test_non_te_unaffected(self) -> None:
        self.gate(None, labels={"bug"})


def _spent() -> bool:
    receipt = pitch_receipt.read(REPO, ISSUE)
    return bool(receipt and receipt.get("consumed_by"))


def _no_gh(*_a, **_k):
    raise AssertionError("a test reached a real gh/git call")


class PostCommentRouteTests(_Receipts):
    """Sticky-wicket PATCH 1: post-comment redirects a Lane 1 amendment to lane-comment."""

    def route(self, labels, comments, lane="1"):
        with patch.dict(os.environ, {"LANE": lane}), \
                patch.object(post_lane1_issue, "run", side_effect=_no_gh), \
                patch.object(post_lane1_issue, "issue_labels", return_value=labels), \
                patch.object(post_lane1_issue, "issue_comments", return_value=comments) as read:
            post_comment.refuse_amendment_route(REPO, ISSUE)
        return read

    def test_refused_on_te_issue_with_posted_handoff(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            self.route(TE, HANDOFF)
        self.assertIn("lane-comment -- --kind discussion --amendment {design,wording}",
                      str(caught.exception.code))

    def test_lane_unset_is_refused_too(self) -> None:
        with patch.dict(os.environ, {}, clear=False) as env:
            env.pop("LANE", None)
            with patch.object(post_lane1_issue, "run", side_effect=_no_gh), \
                    patch.object(post_lane1_issue, "issue_labels", return_value=TE), \
                    patch.object(post_lane1_issue, "issue_comments", return_value=HANDOFF):
                with self.assertRaises(SystemExit):
                    post_comment.refuse_amendment_route(REPO, ISSUE)

    def test_posts_on_non_te_issue(self) -> None:
        read = self.route({"bug"}, HANDOFF)
        read.assert_not_called()

    def test_posts_on_te_issue_before_any_handoff(self) -> None:
        self.route(TE, NO_HANDOFF)

    def test_lane_two_never_reads(self) -> None:
        with patch.dict(os.environ, {"LANE": "2"}), \
                patch.object(post_lane1_issue, "issue_labels", side_effect=_no_gh):
            post_comment.refuse_amendment_route(REPO, ISSUE)

    def test_main_refuses_before_posting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            body.write_text("a design change\n")
            argv = ["post_comment.py", "--repo", REPO, "--issue", str(ISSUE), "--file", str(body)]
            with patch.object(sys, "argv", argv), patch.dict(os.environ, {"LANE": "1"}), \
                    patch.object(post_comment, "apply_project_identity"), \
                    patch.object(post_lane1_issue, "run", side_effect=_no_gh), \
                    patch.object(post_lane1_issue, "issue_labels", return_value=TE), \
                    patch.object(post_lane1_issue, "issue_comments", return_value=HANDOFF), \
                    patch.object(post_comment, "post_comment") as posted:
                with self.assertRaises(SystemExit):
                    post_comment.main()
            posted.assert_not_called()


class L1PostConsumeOrderTests(_Receipts):
    """Sticky-wicket PATCH 2: the verdict is spent before the fallible receipt write."""

    def post(self, kind, **kwargs):
        def broken_receipt(_record):
            raise OSError("disk full")
        with patch.object(l1_post, "run", side_effect=_no_gh), \
                patch.object(l1_post, "comment_body", return_value=("https://example/c#1", 1)), \
                patch.object(l1_post, "write_receipt", side_effect=broken_receipt), \
                patch.object(l1_post, "validate_sticky_wicket"), \
                patch.object(l1_post, "world_checks", return_value=([], [])), \
                patch.object(l1_post, "_afk_toggle", None), \
                patch.object(l1_post, "_tasklist_owed", None), \
                patch.object(l1_post._route_snapshot, "snapshot", return_value=("<!-- route -->", None)), \
                patch.object(l1_post.route_store, "record_floor"):
            with self.assertRaises(OSError):
                l1_post.post_kind(REPO, ISSUE, kind, "body", "abc", "br", **kwargs)

    def test_correction_receipt_write_raises_pitch_still_spent(self) -> None:
        self.fresh()
        l1_post._CORRECTION_GATED[(REPO, ISSUE)] = True
        self.addCleanup(l1_post._CORRECTION_GATED.pop, (REPO, ISSUE), None)
        self.post("rework", rework_kind="correction", amendment="design")
        self.assertTrue(_spent())

    def test_handoff_receipt_write_raises_pitch_still_spent(self) -> None:
        self.fresh()
        self.post("handoff", plan_first=False, labels=TE)
        self.assertTrue(_spent())


class RouteSpendsReceiptTests(_Receipts):
    """Sticky-wicket PATCH 4: after a design post on each route, the receipt is spent."""

    def test_discussion_route_spends_the_receipt(self) -> None:
        self.fresh()
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            body.write_text("A design change to the handoff.\n")
            argv = ["post_lane_discussion.py", "--repo", REPO, "--issue", str(ISSUE),
                    "--file", str(body), "--kind", "discussion", "--amendment", "design"]
            with patch.object(sys, "argv", argv), patch.dict(os.environ, {"LANE": "1"}), \
                    patch.object(post_lane_discussion, "apply_project_identity"), \
                    patch.object(post_lane_discussion, "require_green_ci", return_value=False), \
                    patch.object(post_lane_discussion, "require_round_approval"), \
                    patch.object(post_lane_discussion, "issue_labels", return_value=TE), \
                    patch.object(post_lane_discussion.check_lane3_ready, "fetch_comments",
                                 return_value=HANDOFF), \
                    patch.object(post_lane_discussion, "comment_body",
                                 return_value=("https://example/d#1", 1)), \
                    patch.object(post_lane_discussion, "_afk_toggle", None), \
                    patch.object(post_lane_discussion, "belt_candidates"):
                post_lane_discussion.main()
        self.assertTrue(_spent())

    def test_issue_body_route_spends_the_receipt(self) -> None:
        self.fresh()
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            body.write_text("## Problem\n\nA redesigned body.\n")
            argv = ["post_lane1_issue.py", "--repo", REPO, "--issue", str(ISSUE),
                    "--body-file", str(body), "--amendment", "design"]
            with patch.object(sys, "argv", argv), patch.dict(os.environ, {"LANE": "1"}), \
                    patch.object(post_lane1_issue, "run", side_effect=_no_gh), \
                    patch.object(post_lane1_issue, "resolve_repo", return_value=REPO), \
                    patch.object(post_lane1_issue, "apply_project_identity"), \
                    patch.object(post_lane1_issue, "issue_body", return_value="An existing body.\n"), \
                    patch.object(post_lane1_issue, "validate_amendment"), \
                    patch.object(post_lane1_issue, "issue_labels", return_value=TE), \
                    patch.object(post_lane1_issue, "issue_comments", return_value=HANDOFF), \
                    patch.object(post_lane1_issue, "edit_issue_body"):
                post_lane1_issue.main()
        self.assertTrue(_spent())

    def test_correction_rework_route_spends_the_receipt(self) -> None:
        self.fresh()
        with patch.object(l1_post, "check_lane3_ready_fetch_comments", return_value=HANDOFF):
            l1_post.validate_correction_amendment(REPO, ISSUE, TE, "design")
        self.addCleanup(l1_post._CORRECTION_GATED.pop, (REPO, ISSUE), None)
        with patch.object(l1_post, "run", side_effect=_no_gh), \
                patch.object(l1_post, "comment_body", return_value=("https://example/r#1", 1)), \
                patch.object(l1_post, "write_receipt"), \
                patch.object(l1_post, "validate_sticky_wicket"), \
                patch.object(l1_post, "world_checks", return_value=([], [])), \
                patch.object(l1_post, "_afk_toggle", None), \
                patch.object(l1_post, "_tasklist_owed", None), \
                patch.object(l1_post.route_store, "record_floor"):
            l1_post.post_kind(REPO, ISSUE, "rework", "body", "abc", "br",
                              rework_kind="correction", amendment="design")
        self.assertTrue(_spent())


class PrecloseReaderHuntTests(unittest.TestCase):
    def test_agent_hunts_changed_contract_readers(self) -> None:
        text = (HERE.parent.parent / "agents" / "preclose-inspection.md").read_text(encoding="utf-8")
        self.assertIn("**Changed contracts and all their readers**", text)
        self.assertIn("git grep", text)


if __name__ == "__main__":
    unittest.main()
