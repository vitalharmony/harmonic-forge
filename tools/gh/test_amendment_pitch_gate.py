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


class PrecloseReaderHuntTests(unittest.TestCase):
    def test_agent_hunts_changed_contract_readers(self) -> None:
        text = (HERE.parent.parent / "agents" / "preclose-inspection.md").read_text(encoding="utf-8")
        self.assertIn("**Changed contracts and all their readers**", text)
        self.assertIn("git grep", text)


if __name__ == "__main__":
    unittest.main()
