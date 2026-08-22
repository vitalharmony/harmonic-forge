#!/usr/bin/env python3
"""Tests for batch_auth.py (harmonic-forge#336)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOK_DIR))
import batch_auth as ba  # noqa: E402


class StateFixture(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmpdir.name) / "batch-authorized.json"

    def tearDown(self):
        self.tmpdir.cleanup()


class AuthorizeTests(StateFixture):
    def test_writes_one_entry_per_key(self):
        ba.authorize(["h395", "f334"], "gh pr merge", state_path=self.state_path)
        state = ba._load(self.state_path)
        self.assertIn("H395", state)
        self.assertIn("F334", state)
        self.assertFalse(state["H395"]["consumed"])

    def test_rejects_a_malformed_key(self):
        with self.assertRaises(ValueError):
            ba.authorize(["not-a-key"], "gh issue close", state_path=self.state_path)

    def test_link_pr_requires_prior_authorization(self):
        with self.assertRaises(ValueError):
            ba.link_pr("H999", "vitalharmony/hrse", 1202, state_path=self.state_path)

    def test_link_pr_records_repo_and_number(self):
        ba.authorize(["H395"], "gh pr merge", state_path=self.state_path)
        ba.link_pr("h395", "vitalharmony/hrse", 1202, state_path=self.state_path)
        state = ba._load(self.state_path)
        self.assertEqual(state["H395"]["repo"], "vitalharmony/hrse")
        self.assertEqual(state["H395"]["pr_number"], 1202)


class IssueCloseMatchTests(StateFixture):
    def setUp(self):
        super().setUp()
        ba.authorize(["H395"], "gh issue close", state_path=self.state_path)

    def test_rest_form_matches(self):
        matched, reason = ba.check_and_consume(
            "gh api repos/vitalharmony/hrse/issues/395 -X PATCH -f state=closed",
            state_path=self.state_path,
        )
        self.assertTrue(matched)
        self.assertIn("H395", reason)

    def test_cli_form_with_repo_flag_matches(self):
        matched, _ = ba.check_and_consume(
            "gh issue close 395 --repo vitalharmony/hrse", state_path=self.state_path
        )
        self.assertTrue(matched)

    def test_cli_form_without_repo_flag_does_not_match(self):
        """No repo to resolve a prefix from -- fails closed, not by guessing."""
        matched, _ = ba.check_and_consume("gh issue close 395", state_path=self.state_path)
        self.assertFalse(matched)

    def test_wrong_issue_number_does_not_match(self):
        matched, _ = ba.check_and_consume(
            "gh api repos/vitalharmony/hrse/issues/999 -X PATCH -f state=closed",
            state_path=self.state_path,
        )
        self.assertFalse(matched)

    def test_wrong_repo_does_not_match(self):
        matched, _ = ba.check_and_consume(
            "gh api repos/vitalharmony/harmonic-forge/issues/395 -X PATCH -f state=closed",
            state_path=self.state_path,
        )
        self.assertFalse(matched)

    def test_an_entry_authorized_for_merge_does_not_cover_close(self):
        ba.authorize(["H400"], "gh pr merge", state_path=self.state_path)
        matched, _ = ba.check_and_consume(
            "gh api repos/vitalharmony/hrse/issues/400 -X PATCH -f state=closed",
            state_path=self.state_path,
        )
        self.assertFalse(matched)


class PrMergeMatchTests(StateFixture):
    def setUp(self):
        super().setUp()
        ba.authorize(["H395"], "gh pr merge", state_path=self.state_path)
        ba.link_pr("H395", "vitalharmony/hrse", 1202, state_path=self.state_path)

    def test_rest_form_matches(self):
        matched, reason = ba.check_and_consume(
            "gh api -X PUT repos/vitalharmony/hrse/pulls/1202/merge -f delete_branch=true",
            state_path=self.state_path,
        )
        self.assertTrue(matched)
        self.assertIn("H395", reason)

    def test_cli_form_with_repo_flag_matches(self):
        matched, _ = ba.check_and_consume(
            "gh pr merge 1202 --repo vitalharmony/hrse --squash", state_path=self.state_path
        )
        self.assertTrue(matched)

    def test_unlinked_pr_number_does_not_match(self):
        """The PR-number gap: without link_pr(), no PR merge ever matches."""
        ba.authorize(["F334"], "gh pr merge", state_path=self.state_path)
        matched, _ = ba.check_and_consume(
            "gh pr merge 5001 --repo vitalharmony/harmonic-forge", state_path=self.state_path
        )
        self.assertFalse(matched)

    def test_wrong_pr_number_does_not_match(self):
        matched, _ = ba.check_and_consume(
            "gh pr merge 9999 --repo vitalharmony/hrse", state_path=self.state_path
        )
        self.assertFalse(matched)


class ExpiryTests(StateFixture):
    def test_expired_entry_does_not_match(self):
        ba.authorize(["H395"], "gh issue close", ttl_hours=2, state_path=self.state_path)
        state = ba._load(self.state_path)
        state["H395"]["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat()
        ba._save(state, self.state_path)
        matched, _ = ba.check_and_consume(
            "gh api repos/vitalharmony/hrse/issues/395 -X PATCH -f state=closed",
            state_path=self.state_path,
        )
        self.assertFalse(matched)

    def test_live_entry_within_ttl_matches(self):
        ba.authorize(["H395"], "gh issue close", ttl_hours=2, state_path=self.state_path)
        matched, _ = ba.check_and_consume(
            "gh api repos/vitalharmony/hrse/issues/395 -X PATCH -f state=closed",
            state_path=self.state_path,
        )
        self.assertTrue(matched)


class ConsumptionTests(StateFixture):
    def setUp(self):
        super().setUp()
        ba.authorize(["H395"], "gh issue close", state_path=self.state_path)
        self.command = "gh api repos/vitalharmony/hrse/issues/395 -X PATCH -f state=closed"

    def test_a_second_identical_command_still_matches(self):
        """Idempotent per command hash -- hook order independence."""
        first, _ = ba.check_and_consume(self.command, state_path=self.state_path)
        second, _ = ba.check_and_consume(self.command, state_path=self.state_path)
        self.assertTrue(first)
        self.assertTrue(second)

    def test_a_different_command_after_consumption_does_not_match(self):
        ba.check_and_consume(self.command, state_path=self.state_path)
        other_command = self.command.replace("state=closed", "state=closed ")
        matched, _ = ba.check_and_consume(other_command, state_path=self.state_path)
        self.assertFalse(matched)

    def test_consumed_flag_is_set_after_first_match(self):
        ba.check_and_consume(self.command, state_path=self.state_path)
        state = ba._load(self.state_path)
        self.assertTrue(state["H395"]["consumed"])
        self.assertIsNotNone(state["H395"]["consumed_by"])


class NotDataTests(StateFixture):
    def setUp(self):
        super().setUp()
        ba.authorize(["H395"], "gh issue close", state_path=self.state_path)
        ba.authorize(["H400"], "gh pr merge", state_path=self.state_path)
        ba.link_pr("H400", "vitalharmony/hrse", 1202, state_path=self.state_path)

    def test_git_clean_never_matches(self):
        """Never a BATCH target -- the ask rule always fires for it."""
        matched, _ = ba.check_and_consume("git clean -fd", state_path=self.state_path)
        self.assertFalse(matched)

    def test_empty_state_file_short_circuits(self):
        empty = Path(self.tmpdir.name) / "empty.json"
        matched, _ = ba.check_and_consume(
            "gh api repos/vitalharmony/hrse/issues/395 -X PATCH -f state=closed",
            state_path=empty,
        )
        self.assertFalse(matched)

    def test_ordinary_reads_never_match(self):
        for cmd in ("git status", "gh issue list --repo vitalharmony/hrse", "ls -la"):
            with self.subTest(cmd=cmd):
                matched, _ = ba.check_and_consume(cmd, state_path=self.state_path)
                self.assertFalse(matched)

    def test_a_quoted_mention_is_not_an_invocation(self):
        matched, _ = ba.check_and_consume(
            "echo 'gh issue close 395' > notes.md", state_path=self.state_path
        )
        self.assertFalse(matched)


if __name__ == "__main__":
    unittest.main()
