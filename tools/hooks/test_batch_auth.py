#!/usr/bin/env python3
"""Tests for batch_auth.py (harmonic-forge#336, reforged design;
multi-target state shape from harmonic-forge#356 gap 2).

`decide()` is the sole gate for `gh issue close`/`gh pr merge`, so these
tests cover all three outcomes -- allow, ask, and silent (not a covered
command) -- plus the fail-toward-ask contract on anything unparseable or
unclassifiable, plus the two independently-consumable targets per key.
"""

from __future__ import annotations

import fcntl
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

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
    def test_default_authorizes_both_actions(self):
        """harmonic-forge#356 gap 2: one BATCH grant covers implement ->
        merge -> close without a second authorize() call."""
        ba.authorize(["H395"], state_path=self.state_path)
        state = ba._load(self.state_path)
        actions = {t["action"] for t in state["H395"]["targets"]}
        self.assertEqual(actions, {"gh pr merge", "gh issue close"})

    def test_writes_one_entry_per_key(self):
        ba.authorize(["h395", "f334"], state_path=self.state_path)
        state = ba._load(self.state_path)
        self.assertIn("H395", state)
        self.assertIn("F334", state)
        self.assertTrue(all(not t["consumed"] for t in state["H395"]["targets"]))

    def test_narrower_action_list_is_still_supported(self):
        """The H767 case this gap was found from: an issue closed without
        ever having a PR needs only the close target."""
        ba.authorize(["H767"], ["gh issue close"], state_path=self.state_path)
        state = ba._load(self.state_path)
        actions = [t["action"] for t in state["H767"]["targets"]]
        self.assertEqual(actions, ["gh issue close"])

    def test_empty_actions_list_rejected(self):
        with self.assertRaises(ValueError):
            ba.authorize(["H395"], [], state_path=self.state_path)

    def test_rejects_a_malformed_key(self):
        with self.assertRaises(ValueError):
            ba.authorize(["not-a-key"], state_path=self.state_path)

    def test_repeat_authorize_replaces_not_merges(self):
        """A fresh BATCH grant is a new grant -- prior consumption on that
        key must not survive a re-authorize."""
        ba.authorize(["H395"], state_path=self.state_path)
        state = ba._load(self.state_path)
        state["H395"]["targets"][0]["consumed"] = True
        ba._save(state, self.state_path)
        ba.authorize(["H395"], state_path=self.state_path)
        state = ba._load(self.state_path)
        self.assertTrue(all(not t["consumed"] for t in state["H395"]["targets"]))

    def test_link_pr_requires_prior_authorization(self):
        with self.assertRaises(ValueError):
            ba.link_pr("H999", "vitalharmony/hrse", 1202, state_path=self.state_path)

    def test_link_pr_requires_a_merge_target(self):
        ba.authorize(["H767"], ["gh issue close"], state_path=self.state_path)
        with self.assertRaises(ValueError):
            ba.link_pr("H767", "vitalharmony/hrse", 1202, state_path=self.state_path)

    def test_link_pr_records_repo_and_number_on_the_merge_target_only(self):
        ba.authorize(["H395"], state_path=self.state_path)
        ba.link_pr("h395", "vitalharmony/hrse", 1202, state_path=self.state_path)
        state = ba._load(self.state_path)
        merge_target = next(t for t in state["H395"]["targets"] if t["action"] == "gh pr merge")
        close_target = next(t for t in state["H395"]["targets"] if t["action"] == "gh issue close")
        self.assertEqual(merge_target["repo"], "vitalharmony/hrse")
        self.assertEqual(merge_target["pr_number"], 1202)
        self.assertIsNone(close_target["repo"])
        self.assertIsNone(close_target["pr_number"])


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
        ba.authorize(["H395"], state_path=self.state_path)
        result = ba.decide(
            "gh api repos/vitalharmony/hrse/issues/395 -X PATCH -f state=closed",
            state_path=self.state_path,
        )
        self.assertEqual(result[0], "allow")
        self.assertIn("H395", result[1])

    def test_pr_merge_allowed_under_live_authorization_and_link(self):
        ba.authorize(["H395"], state_path=self.state_path)
        ba.link_pr("H395", "vitalharmony/hrse", 1202, state_path=self.state_path)
        result = ba.decide(
            "gh pr merge 1202 --repo vitalharmony/hrse --squash", state_path=self.state_path
        )
        self.assertEqual(result[0], "allow")
        self.assertIn("H395", result[1])

    def test_merge_then_close_both_allowed_from_one_authorize_call(self):
        """The gap 2 scenario end to end: one authorize(), merge consumes
        only the merge target, close still allows independently."""
        ba.authorize(["H395"], state_path=self.state_path)
        ba.link_pr("H395", "vitalharmony/hrse", 1202, state_path=self.state_path)
        merge_result = ba.decide(
            "gh pr merge 1202 --repo vitalharmony/hrse --squash", state_path=self.state_path
        )
        close_result = ba.decide(
            "gh api repos/vitalharmony/hrse/issues/395 -X PATCH -f state=closed",
            state_path=self.state_path,
        )
        self.assertEqual(merge_result[0], "allow")
        self.assertEqual(close_result[0], "allow")

    def test_narrow_close_only_authorization_does_not_grant_merge(self):
        ba.authorize(["H767"], ["gh issue close"], state_path=self.state_path)
        result = ba.decide(
            "gh api repos/vitalharmony/hrse/issues/767 -X PATCH -f state=closed",
            state_path=self.state_path,
        )
        self.assertEqual(result[0], "allow")


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
        ba.authorize(["H395"], state_path=self.state_path)
        result = ba.decide("gh issue close 395", state_path=self.state_path)  # no --repo
        self.assertEqual(result[0], "ask")

    def test_unlinked_pr_asks(self):
        """The PR-number gap: without link_pr(), no PR merge ever allows."""
        ba.authorize(["F334"], state_path=self.state_path)
        result = ba.decide(
            "gh pr merge 5001 --repo vitalharmony/harmonic-forge", state_path=self.state_path
        )
        self.assertEqual(result[0], "ask")

    def test_expired_authorization_asks(self):
        ba.authorize(["H395"], ttl_hours=2, state_path=self.state_path)
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

    def test_expiry_applies_to_both_targets_together(self):
        """One expires_at per key, shared by all its targets -- an expired
        entry asks for merge and close alike."""
        ba.authorize(["H395"], state_path=self.state_path)
        ba.link_pr("H395", "vitalharmony/hrse", 1202, state_path=self.state_path)
        state = ba._load(self.state_path)
        state["H395"]["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat()
        ba._save(state, self.state_path)
        merge_result = ba.decide(
            "gh pr merge 1202 --repo vitalharmony/hrse", state_path=self.state_path
        )
        close_result = ba.decide(
            "gh api repos/vitalharmony/hrse/issues/395 -X PATCH -f state=closed",
            state_path=self.state_path,
        )
        self.assertEqual(merge_result[0], "ask")
        self.assertEqual(close_result[0], "ask")

    def test_wrong_issue_number_asks_not_silent(self):
        ba.authorize(["H395"], state_path=self.state_path)
        result = ba.decide(
            "gh api repos/vitalharmony/hrse/issues/999 -X PATCH -f state=closed",
            state_path=self.state_path,
        )
        self.assertEqual(result[0], "ask")

    def test_an_entry_authorized_for_merge_only_does_not_cover_close(self):
        ba.authorize(["H400"], ["gh pr merge"], state_path=self.state_path)
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
        ba.authorize(["H395"], state_path=self.state_path)
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

    def test_consumed_flag_is_set_on_the_close_target_only(self):
        """Consuming the close target must not mark the merge target
        consumed -- they are independent (harmonic-forge#356 gap 2)."""
        ba.decide(self.command, state_path=self.state_path)
        state = ba._load(self.state_path)
        close_target = next(t for t in state["H395"]["targets"] if t["action"] == "gh issue close")
        merge_target = next(t for t in state["H395"]["targets"] if t["action"] == "gh pr merge")
        self.assertTrue(close_target["consumed"])
        self.assertIsNotNone(close_target["consumed_by"])
        self.assertFalse(merge_target["consumed"])


class GraphQLProtectionTests(StateFixture):
    """harmonic-forge#369, AC1/item 1: a GraphQL close/merge mutation is
    ALWAYS `ask`, never `allow` -- Lane 1's settled decision 1, dropping the
    node-ID linking design entirely. Opacity (an uninspectable document) is
    itself the signal, independent of any specific mutation name."""

    def test_protected_mutations_always_ask(self):
        for mutation in (
            "closeIssue", "mergePullRequest", "updateIssue",
            "enablePullRequestAutoMerge", "enqueuePullRequest", "closePullRequest",
        ):
            with self.subTest(mutation=mutation):
                cmd = f"gh api graphql -f query='mutation {{ {mutation}(input: {{}}) {{ clientMutationId }} }}'"
                result = ba.decide(cmd, state_path=self.state_path)
                self.assertEqual(result[0], "ask", cmd)

    def test_opaque_at_file_query_asks(self):
        result = ba.decide("gh api graphql -F query=@close.graphql", state_path=self.state_path)
        self.assertEqual(result[0], "ask")

    def test_opaque_unresolved_variable_asks_even_with_no_protected_name(self):
        """Opacity itself is the signal (Lane 1 spec) -- not conditioned on
        also matching a known mutation name."""
        cmd = "gh api graphql -f query='mutation { someOtherMutation(input: $input) { clientMutationId } }'"
        result = ba.decide(cmd, state_path=self.state_path)
        self.assertEqual(result[0], "ask")

    def test_missing_query_document_asks(self):
        result = ba.decide("gh api graphql", state_path=self.state_path)
        self.assertEqual(result[0], "ask")

    def test_routine_board_write_query_is_not_classified(self):
        """A normal, inspectable, unprotected GraphQL board write must stay
        silent (`None`) -- the fix must not turn routine work into a prompt."""
        cmd = "gh api graphql -f query='mutation { updateProjectV2ItemFieldValue(input: {}) { clientMutationId } }'"
        self.assertIsNone(ba.decide(cmd, state_path=self.state_path))

    def test_case_sensitive_match_does_not_false_positive_on_close_references(self):
        """addCloseIssueReferences/removeCloseIssueReferences are real,
        benign mutations distinct from closeIssue -- word-boundary + case
        sensitivity must not treat them as protected."""
        cmd = "gh api graphql -f query='mutation { addCloseIssueReferences(input: {}) { clientMutationId } }'"
        self.assertIsNone(ba.decide(cmd, state_path=self.state_path))


class InvocationPrefixTests(StateFixture):
    """harmonic-forge#369, item 2: `env`/leading-assignment/`command`/
    `nohup` prefixed `gh` forms must not bypass classification."""

    def test_prefixed_close_and_merge_ask_without_a_grant(self):
        for cmd in (
            "env GH_HOST=x gh issue close 700 --repo vitalharmony/hrse",
            "env -i gh issue close 700 --repo vitalharmony/hrse",
            "env -u FOO gh issue close 700 --repo vitalharmony/hrse",
            "command gh issue close 700 --repo vitalharmony/hrse",
            "nohup gh issue close 700 --repo vitalharmony/hrse",
            "VAR=x gh pr merge 993 --repo vitalharmony/hrse",
        ):
            with self.subTest(cmd=cmd):
                result = ba.decide(cmd, state_path=self.state_path)
                self.assertEqual(result[0], "ask", cmd)

    def test_prefixed_close_allows_with_a_grant(self):
        for cmd in (
            "env GH_HOST=x gh issue close 700 --repo vitalharmony/hrse",
            "command gh issue close 700 --repo vitalharmony/hrse",
            "nohup gh issue close 700 --repo vitalharmony/hrse",
        ):
            with self.subTest(cmd=cmd):
                ba.authorize(["H700"], ["gh issue close"], state_path=self.state_path)
                result = ba.decide(cmd, state_path=self.state_path)
                self.assertEqual(result[0], "allow", cmd)


class LockingTests(StateFixture):
    """harmonic-forge#369: the read -> live-entry-check -> consume/write
    sequence is now guarded by a non-blocking, sub-second file lock. Both
    the fail-closed-on-contention behavior and the race it closes are
    asserted directly, not just the mechanism's presence."""

    def test_stale_lock_makes_decide_ask_promptly_not_hang(self):
        ba.authorize(["H600"], ["gh issue close"], state_path=self.state_path)
        lock_path = self.state_path.with_name(self.state_path.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        holder_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(holder_fd, fcntl.LOCK_EX)
        try:
            start = time.monotonic()
            result = ba.decide(
                "gh api repos/vitalharmony/hrse/issues/600 -X PATCH -f state=closed",
                state_path=self.state_path,
            )
            elapsed = time.monotonic() - start
        finally:
            fcntl.flock(holder_fd, fcntl.LOCK_UN)
            os.close(holder_fd)

        self.assertEqual(result[0], "ask")
        self.assertIn("lock", result[1].lower())
        self.assertLess(elapsed, 1.0, "decide() must fail closed promptly, never hang")

    def test_two_commands_racing_for_one_target_do_not_both_allow(self):
        """The exact hazard this issue was filed for: an unlocked
        read-modify-write could let two racing commands both observe
        'not yet consumed' and both write, granting more than the one
        intended one-shot use. Forces the race deterministically by
        gating the in-lock write on a second thread actually starting."""
        ba.authorize(["H500"], ["gh issue close"], state_path=self.state_path)
        base_cmd = "gh api repos/vitalharmony/hrse/issues/500 -X PATCH -f state=closed"
        cmd_a, cmd_b = base_cmd, base_cmd + " "  # distinct hashes, same target

        entered_save = threading.Event()
        release_save = threading.Event()
        real_save = ba._save

        def gated_save(state, state_path=None):
            entered_save.set()
            release_save.wait(timeout=2)
            real_save(state, state_path)

        results: dict[str, tuple] = {}

        def run_a():
            with mock.patch.object(ba, "_save", gated_save):
                results["a"] = ba.decide(cmd_a, state_path=self.state_path)

        def run_b():
            results["b"] = ba.decide(cmd_b, state_path=self.state_path)

        thread_a = threading.Thread(target=run_a)
        thread_a.start()
        self.assertTrue(entered_save.wait(timeout=2), "thread A never reached its write")

        thread_b = threading.Thread(target=run_b)
        thread_b.start()
        time.sleep(0.1)  # a real window for B to race in if the lock did nothing
        release_save.set()
        thread_a.join(timeout=2)
        thread_b.join(timeout=2)

        self.assertEqual(
            sorted(r[0] for r in results.values()), ["allow", "ask"],
            f"exactly one racing command may consume the one-shot target: {results}",
        )


if __name__ == "__main__":
    unittest.main()
