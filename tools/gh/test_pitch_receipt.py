#!/usr/bin/env python3
"""harmonic-forge#838 AC3/AC7: pitch-inspection receipts for Tooling Exception
handoffs, and l1_post's refusal without one."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import pitch_receipt  # noqa: E402
from test_l1_post_handoff import post  # noqa: E402

REPO, ISSUE = "vitalharmony/harmonic-forge", 9838
TE = {"tooling", "tooling-exception"}


class ReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(pitch_receipt, "receipt_dir", return_value=Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_unlabeled_issue_needs_no_receipt(self) -> None:
        self.assertIsNone(pitch_receipt.refusal(REPO, ISSUE, {"bug"}))

    def test_labeled_issue_without_a_receipt_is_refused_with_the_command(self) -> None:
        reason = pitch_receipt.refusal(REPO, ISSUE, TE)
        self.assertIn("pitch_receipt.py record", reason)
        self.assertIn("trigger 4", reason)

    def test_proceed_verdicts_allow_posting(self) -> None:
        for verdict in ("PROCEED", "PROCEED_WITH_NAMED_CHANGES"):
            pitch_receipt.record(REPO, ISSUE, verdict)
            self.assertIsNone(pitch_receipt.refusal(REPO, ISSUE, TE))

    def test_reforge_refuses_until_a_new_verdict_is_recorded(self) -> None:
        pitch_receipt.record(REPO, ISSUE, "REFORGE")
        self.assertIn("REFORGE", pitch_receipt.refusal(REPO, ISSUE, TE))
        pitch_receipt.record(REPO, ISSUE, "PROCEED")
        self.assertIsNone(pitch_receipt.refusal(REPO, ISSUE, TE))

    def test_waiver_needs_the_operators_reason_and_stores_it(self) -> None:
        with self.assertRaises(SystemExit):
            pitch_receipt.record(REPO, ISSUE, "WAIVED")
        pitch_receipt.record(REPO, ISSUE, "WAIVED", reason="operator: 'waive it, Codex Lane 1'")
        self.assertIsNone(pitch_receipt.refusal(REPO, ISSUE, TE))
        self.assertIn("operator", pitch_receipt.read(REPO, ISSUE)["reason"])

    def test_corrupt_receipt_fails_closed(self) -> None:
        pitch_receipt.receipt_path(REPO, ISSUE).write_text("{not json", encoding="utf-8")
        self.assertIsNotNone(pitch_receipt.refusal(REPO, ISSUE, TE))

    def test_receipt_is_bound_to_the_issue_not_another(self) -> None:
        pitch_receipt.record(REPO, ISSUE + 1, "PROCEED")
        self.assertIsNotNone(pitch_receipt.refusal(REPO, ISSUE, TE))


class L1PostTests(unittest.TestCase):
    """The label is read live at post time: a label added after the draft still
    triggers the check (the handoff's TC6 hard case)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(pitch_receipt, "receipt_dir", return_value=Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_label_read_at_post_time_refuses_without_a_receipt(self) -> None:
        with patch.object(post, "issue_labels", return_value=TE), \
                self.assertRaises(SystemExit):
            post.validate_pitch_inspected(REPO, ISSUE)

    def test_label_read_at_post_time_passes_with_a_receipt(self) -> None:
        pitch_receipt.record(REPO, ISSUE, "PROCEED")
        with patch.object(post, "issue_labels", return_value=TE):
            post.validate_pitch_inspected(REPO, ISSUE)

    def test_unlabeled_issue_posts_without_a_receipt(self) -> None:
        with patch.object(post, "issue_labels", return_value={"feature"}):
            post.validate_pitch_inspected(REPO, ISSUE)


if __name__ == "__main__":
    unittest.main()
