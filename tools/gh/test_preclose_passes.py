#!/usr/bin/env python3
"""harmonic-forge#834 AC7: the per-issue preclose pass cap and patch-id binding.

Runs the real `preclose_check` plan/complete against a scratch git repo
(`ScratchRepo`, from `test_preclose_check.py`) and the real merge hook's
receipt check, so the two enforcement points are proven to agree on one
receipt shape rather than each against its own mock.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "hooks"))

import preclose_passes  # noqa: E402
from test_preclose_check import ANCHORED, ScratchRepo, git, preclose  # noqa: E402

import block_missing_preclose_inspection as hook  # noqa: E402

REPO, ISSUE = "vitalharmony/hrse", 1208
SURVIVOR = [ANCHORED]


class PassCapTests(ScratchRepo):
    def run_pass(self, relpath: str, findings: list | None = None, force: bool = False) -> None:
        self.commit(relpath)
        self.plan(tier="fast", force=force)
        self.complete(findings=findings, not_triggered=bool(findings), force=force)

    def receipt(self) -> dict:
        return preclose.find_receipt(REPO, ISSUE)

    def rebase_onto_moved_base(self) -> None:
        """Main moves under the branch: an unrelated commit on `base`, then a
        rebase. New head SHA, identical `base...head` diff."""
        git("checkout", "-q", "base", cwd=self.repo)
        (self.repo / "unrelated.txt").write_text("u\n")
        git("add", "-A", cwd=self.repo)
        git("commit", "-qm", "unrelated", cwd=self.repo)
        git("checkout", "-q", "main", cwd=self.repo)
        git("rebase", "-q", "base", cwd=self.repo)

    def test_receipt_records_count_survivors_and_patch_id(self) -> None:
        self.run_pass("scripts/a.py", SURVIVOR)
        stored = self.receipt()
        self.assertEqual(stored["pass_count"], 1)
        self.assertEqual(stored["surviving_findings"], 1)
        self.assertEqual(stored["reviewed_patch_id"], preclose.local_patch_id("base", "HEAD"))

    def test_planning_a_second_pass_does_not_reset_the_count(self) -> None:
        self.run_pass("scripts/a.py", SURVIVOR)
        self.commit("scripts/b.py")
        self.plan(tier="fast")
        self.assertEqual(self.receipt()["status"], "planned")
        self.assertEqual(self.receipt()["pass_count"], 1)

    def test_patch_identical_rebase_is_not_a_new_pass(self) -> None:
        self.run_pass("scripts/a.py")
        self.rebase_onto_moved_base()
        with self.assertRaises(SystemExit) as caught:
            self.plan(tier="fast")
        self.assertIn("patch-identical", str(caught.exception))
        self.assertEqual(self.receipt()["pass_count"], 1)

    def test_third_pass_with_survivors_on_both_names_sticky_wicket(self) -> None:
        self.run_pass("scripts/a.py", SURVIVOR)
        self.run_pass("scripts/b.py", SURVIVOR)
        self.commit("scripts/c.py")
        with self.assertRaises(SystemExit) as caught:
            self.plan(tier="fast")
        self.assertIn("sticky-wicket", str(caught.exception))

    def test_third_pass_after_a_clean_pass_escalates_to_the_operator(self) -> None:
        self.run_pass("scripts/a.py", SURVIVOR)
        self.run_pass("scripts/b.py")
        self.commit("scripts/c.py")
        with self.assertRaises(SystemExit) as caught:
            self.plan(tier="fast")
        message = str(caught.exception)
        self.assertIn("Escalate to the operator", message)
        self.assertNotIn("sticky-wicket", message)

    def test_complete_is_capped_too(self) -> None:
        self.run_pass("scripts/a.py", SURVIVOR)
        self.run_pass("scripts/b.py", SURVIVOR)
        self.commit("scripts/c.py")
        with self.assertRaises(SystemExit):
            self.complete(findings=SURVIVOR, not_triggered=True)

    def test_force_bypasses_and_records_pass_three(self) -> None:
        self.run_pass("scripts/a.py", SURVIVOR)
        self.run_pass("scripts/b.py", SURVIVOR)
        self.run_pass("scripts/c.py", SURVIVOR, force=True)
        self.assertEqual(self.receipt()["pass_count"], 3)

    def test_count_survives_an_archive_failure(self) -> None:
        """The archive swallows its own errors (harmonic-forge#826); break its
        import for the whole second pass and the count must still be 2."""
        self.run_pass("scripts/a.py", SURVIVOR)
        with patch.dict(sys.modules, {"archive": None}):
            self.run_pass("scripts/b.py", SURVIVOR)
        self.assertEqual(self.receipt()["pass_count"], 2)

    def test_legacy_receipt_without_a_count_is_one_pass(self) -> None:
        legacy = {"repo": REPO, "issue": ISSUE, "reviewed_sha": "old", "status": "complete",
                  "refuters": 3, "surviving_findings": 2}
        self.assertEqual(len(preclose_passes.history(legacy)), 1)
        self.assertEqual(preclose_passes.record(legacy, "new", "pid", 1)["pass_count"], 2)


class HookPatchIdTests(ScratchRepo):
    """AC3/AC4 through the real hook and the real receipt."""

    def setUp(self) -> None:
        super().setUp()
        self.commit("scripts/a.py")
        self.plan(tier="fast")
        self.complete()
        self.reviewed = preclose.find_receipt(REPO, ISSUE)["reviewed_sha"]

    def pr_diff(self) -> str:
        return preclose.run("git", "diff", "base...HEAD").stdout

    def test_patch_identical_rebase_is_accepted(self) -> None:
        PassCapTests.rebase_onto_moved_base(self)
        with patch.object(hook, "_gh", return_value=self.pr_diff()):
            self.assertTrue(hook._preclose_receipt_ok(REPO, str(ISSUE), "new-head-sha", "7"))

    def test_real_diff_change_is_denied(self) -> None:
        self.commit("scripts/extra.py")
        with patch.object(hook, "_gh", return_value=self.pr_diff()):
            self.assertFalse(hook._preclose_receipt_ok(REPO, str(ISSUE), "new-head-sha", "7"))

    def test_unreadable_pr_diff_fails_closed(self) -> None:
        with patch.object(hook, "_gh", return_value=None):
            self.assertFalse(hook._preclose_receipt_ok(REPO, str(ISSUE), "new-head-sha", "7"))

    def test_stale_message_below_the_cap_still_offers_a_pass(self) -> None:
        self.assertIn("Run the pre-close pass", hook._stale_receipt_message(REPO, str(ISSUE), "7", "abc"))

    def test_stale_message_at_the_cap_never_offers_a_third_pass(self) -> None:
        self.commit("scripts/b.py")
        self.plan(tier="fast")
        self.complete()
        message = hook._stale_receipt_message(REPO, str(ISSUE), "7", "abc")
        self.assertNotIn("Run the pre-close pass", message)
        self.assertIn("Escalate to the operator", message)


if __name__ == "__main__":
    unittest.main()
