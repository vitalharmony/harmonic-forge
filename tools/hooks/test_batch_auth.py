#!/usr/bin/env python3
"""Tests for batch_auth.py (harmonic-forge#336, reforged design).

`decide()` is now the sole gate for `gh issue close`/`gh pr merge`, so
these tests cover all three outcomes -- allow, ask, and silent (not a
covered command) -- plus the fail-toward-ask contract on anything
unparseable or unclassifiable.
"""

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


class ClassifyTests(unittest.TestCase):
    """classify_issue_close / classify_pr_merge recognize the command class
    independent of any authorization state -- these are what makes decide()
    ask (rather than stay silent) on an uncovered/unauthorized command."""

    def test_issue_close_rest_form(self):
        tokens = "gh api repos/vitalharmony/hrse/issues/395 -X PATCH -f state=closed".split()
        self.assertEqual(ba.classify_issue_close(tokens), ("vitalharmony/hrse", "395"))

    def test_issue_close_cli_form_with_repo(self):
        tokens = "gh issue close 395 --repo vitalharmony/hrse".split()
        self.assertEqual(ba.classify_issue_close(tokens), ("vitalharmony/hrse", "395"))

    def test_issue_close_cli_form_without_repo_still_classified(self):
        """Recognized as issue-close even though repo is unresolvable --
        decide() must ask, not silently ignore it."""
        tokens = "gh issue close 395".split()
        self.assertEqual(ba.classify_issue_close(tokens), (None, "395"))

    def test_pr_merge_rest_form(self):
        tokens = "gh api -X PUT repos/vitalharmony/hrse/pulls/1202/merge".split()
        self.assertEqual(ba.classify_pr_merge(tokens), ("vitalharmony/hrse", 1202))

    def test_pr_merge_cli_form_without_delete_branch(self):
        """Every `gh pr merge`, not just --delete-branch, is now covered."""
        tokens = "gh pr merge 993 --repo vitalharmony/hrse --squash".split()
        self.assertEqual(ba.classify_pr_merge(tokens), ("vitalharmony/hrse", 993))

    def test_unrelated_commands_are_not_classified(self):
        for cmd in ("git status", "gh issue list --repo vitalharmony/hrse", "git clean -fd"):
            with self.subTest(cmd=cmd):
                tokens = cmd.split()
                self.assertIsNone(ba.classify_issue_close(tokens))
                self.assertIsNone(ba.classify_pr_merge(tokens))


class DecideAllowTests(StateFixture):
    def test_issue_close_allowed_under_live_authorization(self):
        ba.authorize(["H395"], "gh issue close", state_path=self.state_path)
        result = ba.decide(
            "gh api repos/vitalharmony/hrse/issues/395 -X PATCH -f state=closed",
            state_path=self.state_path,
        )
        self.assertEqual(result[0], "allow")
        self.assertIn("H395", result[1])

    def test_pr_merge_allowed_under_live_authorization_and_link(self):
        ba.authorize(["H395"], "gh pr merge", state_path=self.state_path)
        ba.link_pr("H395", "vitalharmony/hrse", 1202, state_path=self.state_path)
        result = ba.decide(
            "gh pr merge 1202 --repo vitalharmony/hrse --squash", state_path=self.state_path
        )
        self.assertEqual(result[0], "allow")
        self.assertIn("H395", result[1])


class DecideAskTests(StateFixture):
    def test_issue_close_with_no_authorization_asks(self):
        result = ba.decide(
            "gh api repos/vitalharmony/hrse/issues/395 -X PATCH -f state=closed",
            state_path=self.state_path,
        )
        self.assertEqual(result[0], "ask")

    def test_pr_merge_with_no_authorization_asks(self):
        result = ba.decide(
            "gh pr merge 993 --repo vitalharmony/hrse --squash", state_path=self.state_path
        )
        self.assertEqual(result[0], "ask")

    def test_pr_merge_delete_branch_reason_mentions_stacked_child(self):
        result = ba.decide(
            "gh pr merge 993 --repo vitalharmony/hrse --delete-branch",
            state_path=self.state_path,
        )
        self.assertEqual(result[0], "ask")
        self.assertIn("stacked child", result[1])

    def test_unresolvable_repo_asks_rather_than_silently_allowing(self):
        ba.authorize(["H395"], "gh issue close", state_path=self.state_path)
        result = ba.decide("gh issue close 395", state_path=self.state_path)  # no --repo
        self.assertEqual(result[0], "ask")

    def test_unlinked_pr_asks(self):
        """The PR-number gap: without link_pr(), no PR merge ever allows."""
        ba.authorize(["F334"], "gh pr merge", state_path=self.state_path)
        result = ba.decide(
            "gh pr merge 5001 --repo vitalharmony/harmonic-forge", state_path=self.state_path
        )
        self.assertEqual(result[0], "ask")

    def test_expired_authorization_asks(self):
        ba.authorize(["H395"], "gh issue close", ttl_hours=2, state_path=self.state_path)
        state = ba._load(self.state_path)
        state["H395"]["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat()
        ba._save(state, self.state_path)
        result = ba.decide(
            "gh api repos/vitalharmony/hrse/issues/395 -X PATCH -f state=closed",
            state_path=self.state_path,
        )
        self.assertEqual(result[0], "ask")

    def test_wrong_issue_number_asks_not_silent(self):
        ba.authorize(["H395"], "gh issue close", state_path=self.state_path)
        result = ba.decide(
            "gh api repos/vitalharmony/hrse/issues/999 -X PATCH -f state=closed",
            state_path=self.state_path,
        )
        self.assertEqual(result[0], "ask")

    def test_an_entry_authorized_for_merge_does_not_cover_close(self):
        ba.authorize(["H400"], "gh pr merge", state_path=self.state_path)
        result = ba.decide(
            "gh api repos/vitalharmony/hrse/issues/400 -X PATCH -f state=closed",
            state_path=self.state_path,
        )
        self.assertEqual(result[0], "ask")

    def test_unparseable_command_asks(self):
        result = ba.decide("echo 'unbalanced", state_path=self.state_path)
        self.assertEqual(result[0], "ask")
        self.assertIn("Could not safely parse", result[1])


class DecideSilentTests(StateFixture):
    def test_unrelated_commands_return_none(self):
        for cmd in ("git status", "git clean -fd", "gh issue list --repo vitalharmony/hrse", "ls -la"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(ba.decide(cmd, state_path=self.state_path))

    def test_a_quoted_mention_is_not_an_invocation(self):
        result = ba.decide("echo 'gh issue close 395' > notes.md", state_path=self.state_path)
        self.assertIsNone(result)

    def test_empty_state_file_still_asks_for_a_covered_command(self):
        """No BATCH entries at all is not the same as 'not covered' -- an
        empty state file must still produce an ask for a covered command,
        never a silent pass."""
        empty = Path(self.tmpdir.name) / "empty.json"
        result = ba.decide(
            "gh api repos/vitalharmony/hrse/issues/395 -X PATCH -f state=closed",
            state_path=empty,
        )
        self.assertEqual(result[0], "ask")


class ConsumptionTests(StateFixture):
    def setUp(self):
        super().setUp()
        ba.authorize(["H395"], "gh issue close", state_path=self.state_path)
        self.command = "gh api repos/vitalharmony/hrse/issues/395 -X PATCH -f state=closed"

    def test_a_second_identical_command_still_allows(self):
        """Idempotent per command hash -- hook order independence."""
        first = ba.decide(self.command, state_path=self.state_path)
        second = ba.decide(self.command, state_path=self.state_path)
        self.assertEqual(first[0], "allow")
        self.assertEqual(second[0], "allow")

    def test_a_different_command_after_consumption_asks(self):
        ba.decide(self.command, state_path=self.state_path)
        other_command = self.command.replace("state=closed", "state=closed ")
        result = ba.decide(other_command, state_path=self.state_path)
        self.assertEqual(result[0], "ask")

    def test_consumed_flag_is_set_after_first_allow(self):
        ba.decide(self.command, state_path=self.state_path)
        state = ba._load(self.state_path)
        self.assertTrue(state["H395"]["consumed"])
        self.assertIsNotNone(state["H395"]["consumed_by"])


if __name__ == "__main__":
    unittest.main()
