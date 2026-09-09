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
import json
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


def consume_ok(command, state_path):
    """Consume as `batch_consume.py` does after a command actually ran.

    `landed` is injected: hrse tests must not reach the network, and the
    point under test is the bookkeeping, not the GitHub query.
    """
    return ba.consume(command, state_path=state_path, landed=lambda *a, **k: True)


def consumed_one(command, state_path):
    """`consume_ok` for the single-action case: assert exactly one key, return it."""
    keys = consume_ok(command, state_path)
    assert len(keys) <= 1, f"expected at most one key, got {keys}"
    return keys[0] if keys else None


class StateFixture(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmpdir.name) / "batch-authorized.json"
        # AC5 derivation shells out to `gh` on the no-link path, which most of
        # this suite now reaches. Stub it to "unreadable" by default: a unit
        # suite that makes network calls is slow, flaky, and — worse — would
        # pass or fail on the state of live PRs. Tests that exercise derivation
        # set `self.carriers` explicitly.
        self.carriers = None
        self._real_carriers = ba._pr_carriers
        ba._pr_carriers = lambda repo, number: self.carriers

    def tearDown(self):
        ba._pr_carriers = self._real_carriers
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


class TopUpCliTests(StateFixture):
    """harmonic-forge#549 AC6 — `top_up` is reachable, and it is the safe default.

    Third instance of this epic's own class, in the same module: the safe
    function was written and tested by its callers, and `batch_auth.py --help`
    listed only `{authorize, link-pr}`. So "add an issue to a running batch" —
    the ordinary operator need — had a correct implementation and no way to
    invoke it, and the workaround was `authorize <new keys only>` plus the
    operator remembering never to re-list a live key. A destructive default
    with a memorised guard rail is the shape this epic exists to remove.
    """

    def _targets(self, key="F1"):
        return ba._load(self.state_path)[key]["targets"]

    def test_top_up_on_a_live_key_preserves_consumption_and_links(self):
        """The AC's named assertion, and exactly what `authorize` destroys."""
        ba.authorize(["F1"], state_path=self.state_path)
        ba.link_pr("F1", "o/a", 7, state_path=self.state_path)
        state = ba._load(self.state_path)
        target = next(t for t in state["F1"]["targets"] if "merge" in t["action"])
        target.update(consumed=True, consumed_by="some-hash")
        ba._save(state, self.state_path)

        ba.top_up(["F1"], state_path=self.state_path)

        after = next(t for t in self._targets() if "merge" in t["action"])
        self.assertTrue(after["consumed"], "top_up must not un-consume")
        self.assertEqual(after["consumed_by"], "some-hash")
        self.assertEqual(after["repo"], "o/a")
        self.assertEqual(after["pr_number"], 7)

    def test_authorize_by_contrast_destroys_both(self):
        """States the difference the AC exists to protect, so it cannot rot."""
        ba.authorize(["F1"], state_path=self.state_path)
        ba.link_pr("F1", "o/a", 7, state_path=self.state_path)
        state = ba._load(self.state_path)
        target = next(t for t in state["F1"]["targets"] if "merge" in t["action"])
        target.update(consumed=True, consumed_by="h", repo="o/a", pr_number=7)
        ba._save(state, self.state_path)

        ba.authorize(["F1"], state_path=self.state_path)

        after = next(t for t in self._targets() if "merge" in t["action"])
        self.assertFalse(after["consumed"])
        self.assertIsNone(after["pr_number"])

    def test_top_up_returns_only_the_newly_authorized_keys(self):
        ba.authorize(["F1"], state_path=self.state_path)
        fresh = ba.top_up(["F1", "F2"], state_path=self.state_path)
        self.assertEqual([k.upper() for k in fresh], ["F2"])

    def test_top_up_extends_a_live_expiry(self):
        ba.authorize(["F1"], ttl_hours=1.0, state_path=self.state_path)
        before = ba._load(self.state_path)["F1"]["expires_at"]
        ba.top_up(["F1"], ttl_hours=12.0, state_path=self.state_path)
        after = ba._load(self.state_path)["F1"]["expires_at"]
        self.assertGreater(after, before)

    def test_top_up_authorizes_an_expired_key_fresh(self):
        ba.authorize(["F1"], state_path=self.state_path)
        state = ba._load(self.state_path)
        state["F1"]["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        ba._save(state, self.state_path)
        fresh = ba.top_up(["F1"], state_path=self.state_path)
        self.assertEqual([k.upper() for k in fresh], ["F1"])

    def test_cli_exposes_top_up(self):
        """The defect itself: the subcommand was absent from --help."""
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            with self.assertRaises(SystemExit):
                with mock.patch("sys.argv", ["batch_auth.py", "--help"]):
                    ba._cli()
        self.assertIn("top-up", buffer.getvalue())


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
        """harmonic-forge#552: decide() no longer consumes, so the slot is
        spent by consume() -- the PostToolUse half -- and only then does a
        DIFFERENT command find nothing left."""
        self.assertEqual(ba.decide(self.command, state_path=self.state_path)[0],
                         "allow")
        consume_ok(self.command, self.state_path)
        other_command = self.command.replace("state=closed", "state=closed ")
        result = ba.decide(other_command, state_path=self.state_path)
        self.assertEqual(result[0], "ask")

    def test_consumed_flag_is_set_on_the_close_target_only(self):
        """Consuming the close target must not mark the merge target
        consumed -- they are independent (harmonic-forge#356 gap 2)."""
        consume_ok(self.command, self.state_path)
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

    def test_a_held_lock_does_not_affect_decide_at_all(self):
        """`decide()` is read-only (AC1), so it takes no lock and a held one is
        none of its business.

        This replaces a test asserting the opposite — that a held lock made
        `decide()` ask. That WAS the contract while `decide()` wrote. Once AC5
        put a `gh` call on the decision path, keeping the lock meant one
        session's ~0.7s network round trip blew another session's 0.4s budget
        and produced an unexplained prompt on a fully valid grant: the exact
        symptom #552 exists to remove, reintroduced by its own fix. `_save` is
        a temp-file + atomic `os.replace`, so a lockless read sees the state
        before or after a write, never during.
        """
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

        self.assertEqual(result[0], "allow",
                         "a lock held by another session must not turn a live "
                         "authorization into a prompt")
        self.assertLess(elapsed, 1.0, "decide() must never wait on the lock")

    def test_two_commands_racing_to_consume_one_target_do_not_both_win(self):
        """The same hazard this issue was filed for, moved with the write.

        harmonic-forge#552 made `decide()` read-only, so the read-modify-write
        that could double-spend a one-shot grant now lives in `consume()`. The
        property is unchanged and so is the method: gate the in-lock write on a
        second thread actually starting, so the race is forced rather than
        hoped for. Two DIFFERENT command strings target the same slot —
        identical strings are deliberately idempotent and would prove nothing.
        """
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

        results: dict[str, object] = {}
        landed = lambda *a, **k: True  # noqa: E731

        def run_a():
            with mock.patch.object(ba, "_save", gated_save):
                results["a"] = ba.consume(cmd_a, state_path=self.state_path,
                                          landed=landed)

        def run_b():
            results["b"] = ba.consume(cmd_b, state_path=self.state_path,
                                      landed=landed)

        thread_a = threading.Thread(target=run_a)
        thread_a.start()
        self.assertTrue(entered_save.wait(timeout=2), "thread A never reached its write")

        thread_b = threading.Thread(target=run_b)
        thread_b.start()
        time.sleep(0.1)  # a real window for B to race in if the lock did nothing
        release_save.set()
        thread_a.join(timeout=2)
        thread_b.join(timeout=2)

        winners = [k for k, v in results.items() if v]
        self.assertEqual(
            len(winners), 1,
            f"exactly one racing command may consume the one-shot target: {results}",
        )

        state = ba._load(self.state_path)
        close_targets = [t for t in state["H500"]["targets"]
                         if "close" in t["action"].lower()]
        self.assertEqual([t["consumed"] for t in close_targets], [True],
                         "the one-shot close target is consumed exactly once")


if __name__ == "__main__":
    unittest.main()


class CrossRepoMergeArityTests(unittest.TestCase):
    """AC7/AC8 — a cross-repo issue needs one merge target per repo.

    harmonic-forge#497 needed two (the forge tool and the hrse mise wiring).
    `authorize` granted one, the first merge consumed it, and the second
    correctly fell closed to Ask. Of the five issues in that batch two were
    two-repo, which is the standard shape for a forge tool called from hrse's
    mise.toml.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "state.json"

    def _merge_targets(self, key="F1"):
        state = json.loads(self.tmp.read_text())
        return [t for t in state[key]["targets"]
                if "merge" in t["action"].lower()]

    def test_link_pr_allocates_beyond_the_granted_targets(self):
        ba.authorize(["F1"], actions=["gh pr merge", "gh issue close"],
                  state_path=self.tmp)
        self.assertEqual(len(self._merge_targets()), 1)
        ba.link_pr("F1", "o/a", 1, state_path=self.tmp)
        ba.link_pr("F1", "o/b", 2, state_path=self.tmp)
        ba.link_pr("F1", "o/c", 3, state_path=self.tmp)
        self.assertEqual(len(self._merge_targets()), 3)

    def test_each_allocated_target_authorizes_its_own_merge(self):
        ba.authorize(["F1"], actions=["gh pr merge"], state_path=self.tmp)
        for repo, pr in (("o/a", 1), ("o/b", 2), ("o/c", 3)):
            ba.link_pr("F1", repo, pr, state_path=self.tmp)
            verdict, _ = ba.decide(f"gh pr merge {pr} --repo {repo}",
                                state_path=self.tmp)
            self.assertEqual(verdict, "allow", f"{repo}#{pr}")

    def test_link_pr_is_idempotent(self):
        ba.authorize(["F1"], actions=["gh pr merge"], state_path=self.tmp)
        ba.link_pr("F1", "o/a", 1, state_path=self.tmp)
        ba.link_pr("F1", "o/a", 1, state_path=self.tmp)
        self.assertEqual(len(self._merge_targets()), 1)

    def test_a_close_target_is_never_allocated_a_second_time(self):
        """A close is irreversible and happens once; only merges recur."""
        ba.authorize(["F1"], actions=["gh pr merge", "gh issue close"],
                  state_path=self.tmp)
        ba.link_pr("F1", "o/a", 1, state_path=self.tmp)
        ba.link_pr("F1", "o/b", 2, state_path=self.tmp)
        state = json.loads(self.tmp.read_text())
        closes = [t for t in state["F1"]["targets"]
                  if "close" in t["action"].lower()]
        self.assertEqual(len(closes), 1)

    def test_link_pr_still_refuses_a_key_with_no_merge_action(self):
        ba.authorize(["F1"], actions=["gh issue close"], state_path=self.tmp)
        with self.assertRaises(ValueError):
            ba.link_pr("F1", "o/a", 1, state_path=self.tmp)


class NoExecutionPathTests(StateFixture):
    """harmonic-forge#552's named regression: three ways a command matches and
    never runs, each asserting the slot is **NOT** consumed.

    None of these was expressible against the old design, which is why #552 is
    a design fix rather than a patch: `decide()` wrote the state itself, so any
    test of "matched but did not run" would have had to assert that a
    PreToolUse function had not done the thing it unconditionally did.

    Measured cost of the old behaviour, 2026-09-09: F544 held FOUR consumed
    merge targets, every one pointing at PR 1753, which was still unmerged.
    Four match events, zero executions.
    """

    def setUp(self):
        super().setUp()
        ba.authorize(["H395"], state_path=self.state_path)
        ba.link_pr("H395", "vitalharmony/hrse", 1202, state_path=self.state_path)
        self.command = "gh pr merge 1202 --repo vitalharmony/hrse --squash"

    def _merge_target(self):
        state = ba._load(self.state_path)
        return next(t for t in state["H395"]["targets"]
                    if t["action"] == "gh pr merge")

    def test_denied_by_a_sibling_hook_does_not_consume(self):
        """PreToolUse hooks compose under strongest-decision-wins, so another
        hook's `deny` lands AFTER this one has already returned allow. The
        command never runs; nothing may have been spent."""
        self.assertEqual(ba.decide(self.command, state_path=self.state_path)[0],
                         "allow")
        # A sibling hook denies. `consume()` is never reached, because
        # PostToolUse does not fire for a command that did not execute.
        self.assertFalse(self._merge_target()["consumed"])
        self.assertEqual(
            ba.decide(self.command, state_path=self.state_path)[0], "allow",
            "the grant must survive a denial by another hook")

    def test_declined_at_the_prompt_does_not_consume(self):
        """The operator says no. Same shape: allow was returned, nothing ran.

        This is the path that made every retry start worse off than the last —
        six prompts, six spent slots, zero merges."""
        for _ in range(3):
            self.assertEqual(
                ba.decide(self.command, state_path=self.state_path)[0], "allow")
            self.assertFalse(self._merge_target()["consumed"])

    def test_a_merge_that_ran_and_failed_does_not_consume(self):
        """PostToolUse DOES fire here, so this is the path `consume()` itself
        must get right: it confirms the PR actually merged before marking.
        Consuming unconditionally on PostToolUse would just move the
        over-count rather than fix it."""
        ba.decide(self.command, state_path=self.state_path)
        # `gh pr merge` exited non-zero (not mergeable, conflict, network).
        self.assertEqual(ba.consume(self.command, state_path=self.state_path,
                                    landed=lambda *a, **k: False), [])
        self.assertFalse(self._merge_target()["consumed"])
        self.assertEqual(
            ba.decide(self.command, state_path=self.state_path)[0], "allow",
            "a failed merge must leave the grant usable")

    def test_an_unresolvable_outcome_does_not_consume(self):
        """`action_landed` returns None when it cannot establish the answer.

        An unconsumed live grant costs one extra prompt; a wrongly-consumed one
        costs a stuck batch. The asymmetry decides the default."""
        ba.decide(self.command, state_path=self.state_path)
        self.assertEqual(ba.consume(self.command, state_path=self.state_path,
                                    landed=lambda *a, **k: None), [])
        self.assertFalse(self._merge_target()["consumed"])

    def test_a_merge_that_actually_landed_does_consume(self):
        """The positive case, so the four above cannot pass by never consuming."""
        ba.decide(self.command, state_path=self.state_path)
        self.assertEqual(
            ba.consume(self.command, state_path=self.state_path,
                       landed=lambda *a, **k: True), ["H395"])
        self.assertTrue(self._merge_target()["consumed"])


class AllocationKeyedOnRepoAndPrTests(unittest.TestCase):
    """harmonic-forge#552 AC4 — at most one merge target per (repo, pr_number).

    This class replaces #549's `ConsumedSlotRecoveryTests`, which was written
    against a design where a slot could be consumed WITHOUT the merge having
    happened. #552 removes that state at the root, so "recover from a poisoned
    slot" is no longer a thing to test — and the workaround it needed (skipping
    consumed slots when re-linking) is now actively harmful, because `consumed`
    truthfully means the merge landed and skipping it allocates a duplicate.

    Allocation itself stays. #502 AC8 added it because a cross-repo issue needs
    one merge target per repo and the repo count is not knowable when the
    operator types BATCH. What changed is the trigger: a NEW pair allocates, a
    REPEAT of a known pair reuses, regardless of `consumed`.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "state.json"

    def _merge_targets(self, key="F1"):
        state = json.loads(self.tmp.read_text())
        return [t for t in state[key]["targets"]
                if "merge" in t["action"].lower()]

    def test_relinking_a_merged_pr_does_not_allocate_a_duplicate(self):
        """AC4's named regression, and why the #549 clause had to go with it."""
        ba.authorize(["F1"], actions=["gh pr merge"], state_path=self.tmp)
        ba.link_pr("F1", "o/a", 1, state_path=self.tmp)
        consume_ok("gh pr merge 1 --repo o/a", self.tmp)
        self.assertTrue(self._merge_targets()[0]["consumed"])

        ba.link_pr("F1", "o/a", 1, state_path=self.tmp)
        ba.link_pr("F1", "o/a", 1, state_path=self.tmp)
        self.assertEqual(len(self._merge_targets()), 1,
                         "a known (repo, pr) pair must reuse, never allocate")

    def test_a_new_repo_pr_pair_still_allocates(self):
        """#502 AC8's cross-repo reason is sound and is preserved."""
        ba.authorize(["F1"], actions=["gh pr merge"], state_path=self.tmp)
        for repo, pr in (("o/a", 1), ("o/b", 2), ("o/c", 3)):
            ba.link_pr("F1", repo, pr, state_path=self.tmp)
        self.assertEqual(len(self._merge_targets()), 3)

    def test_never_two_live_targets_for_one_pair(self):
        """The tell F544 showed: four targets, all for PR 1753."""
        ba.authorize(["F1"], actions=["gh pr merge"], state_path=self.tmp)
        for _ in range(5):
            ba.link_pr("F1", "o/a", 1, state_path=self.tmp)
        pairs = [(t["repo"], t["pr_number"]) for t in self._merge_targets()]
        self.assertEqual(len(pairs), len(set(pairs)))

    def test_a_live_slot_is_not_shadowed_by_a_consumed_one(self):
        """_match_pr_merge prefers an unconsumed target for the same pair."""
        ba.authorize(["F1"], actions=["gh pr merge"], state_path=self.tmp)
        state = json.loads(self.tmp.read_text())
        merges = [t for t in state["F1"]["targets"] if "merge" in t["action"].lower()]
        merges[0].update(repo="o/a", pr_number=1, consumed=True,
                         consumed_by="an-earlier-merge")
        state["F1"]["targets"].append({"action": "gh pr merge", "consumed": False,
                                       "consumed_by": None, "pr_number": 1,
                                       "repo": "o/a"})
        self.tmp.write_text(json.dumps(state))
        verdict, reason = ba.decide("gh pr merge 1 --repo o/a", state_path=self.tmp)
        self.assertEqual(verdict, "allow", reason)

    def test_all_slots_consumed_asks(self):
        """Not a widening: spent is still spent for a different command."""
        ba.authorize(["F1"], actions=["gh pr merge"], state_path=self.tmp)
        ba.link_pr("F1", "o/a", 1, state_path=self.tmp)
        consume_ok("gh pr merge 1 --repo o/a", self.tmp)
        verdict, reason = ba.decide("gh pr merge 1 --repo o/a --squash",
                                    state_path=self.tmp)
        self.assertEqual(verdict, "ask", reason)

    def test_an_identical_retry_is_still_idempotent(self):
        ba.authorize(["F1"], actions=["gh pr merge"], state_path=self.tmp)
        ba.link_pr("F1", "o/a", 1, state_path=self.tmp)
        cmd = "gh pr merge 1 --repo o/a"
        self.assertEqual(ba.decide(cmd, state_path=self.tmp)[0], "allow")
        consume_ok(cmd, self.tmp)
        self.assertEqual(ba.decide(cmd, state_path=self.tmp)[0], "allow")

    def test_relink_does_not_touch_an_unrelated_key(self):
        ba.authorize(["F1", "F2"], actions=["gh pr merge"], state_path=self.tmp)
        ba.link_pr("F1", "o/a", 1, state_path=self.tmp)
        consume_ok("gh pr merge 1 --repo o/a", self.tmp)
        ba.link_pr("F1", "o/a", 1, state_path=self.tmp)
        self.assertEqual(len(self._merge_targets("F2")), 1)
        self.assertFalse(self._merge_targets("F2")[0]["consumed"])


class AskDiagnosticTests(unittest.TestCase):
    """AC4 — the four states must be tellable apart from the prompt alone.

    Operator's own words on the incident: "waiting for my ok to merge/close
    OR SOMETHING I COULDN'T TELL WHAT."
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "state.json"
        self.tmp.write_text("{}")

    def _reason(self, command):
        verdict, reason = ba.decide(command, state_path=self.tmp)
        self.assertEqual(verdict, "ask")
        return reason

    def test_no_authorization_says_so(self):
        reason = self._reason("gh issue close 9 --repo vitalharmony/hrse")
        self.assertIn("No authorization exists", reason)

    def test_expired_names_the_expiry_time(self):
        ba.authorize(["H9"], actions=["gh issue close"], ttl_hours=-1,
                  state_path=self.tmp)
        reason = self._reason("gh issue close 9 --repo vitalharmony/hrse")
        self.assertIn("EXPIRED", reason)

    def test_an_unlinked_pr_names_link_pr_and_the_command_to_run(self):
        ba.authorize(["H9"], actions=["gh pr merge"], state_path=self.tmp)
        reason = self._reason("gh pr merge 42 --repo vitalharmony/hrse")
        self.assertIn("not linked", reason)
        self.assertIn("link-pr", reason)
        self.assertIn("--pr 42", reason)

    def test_a_consumed_target_says_consumed_not_missing(self):
        ba.authorize(["H9"], actions=["gh pr merge"], state_path=self.tmp)
        ba.link_pr("H9", "vitalharmony/hrse", 42, state_path=self.tmp)
        self.assertEqual(ba.decide("gh pr merge 42 --repo vitalharmony/hrse",
                                state_path=self.tmp)[0], "allow")
        consume_ok("gh pr merge 42 --repo vitalharmony/hrse", self.tmp)
        reason = self._reason("gh pr merge 42 --repo vitalharmony/hrse --squash")
        self.assertIn("CONSUMED", reason)

    def test_an_unmapped_repo_says_so_instead_of_naming_a_null_key(self):
        """`issue_key` returns None for a repo with no shorthand, and the
        first draft rendered that as "no authorization exists for None --
        issue `BATCH None`", sending the operator to type an impossibility."""
        reason = self._reason("gh issue close 9 --repo someorg/unmapped")
        self.assertIn("no shorthand prefix", reason)
        self.assertNotIn("None", reason)

    def test_the_four_diagnostics_are_mutually_distinguishable(self):
        """The point of AC4: no two states produce the same guidance."""
        seen = set()
        self.tmp.write_text("{}")
        seen.add(self._reason("gh issue close 9 --repo vitalharmony/hrse"))
        ba.authorize(["H9"], actions=["gh pr merge"], state_path=self.tmp)
        seen.add(self._reason("gh pr merge 42 --repo vitalharmony/hrse"))
        ba.link_pr("H9", "vitalharmony/hrse", 42, state_path=self.tmp)
        consume_ok("gh pr merge 42 --repo vitalharmony/hrse", self.tmp)
        seen.add(self._reason("gh pr merge 42 --repo vitalharmony/hrse --squash"))
        ba.authorize(["H8"], actions=["gh issue close"], ttl_hours=-1,
                  state_path=self.tmp)
        seen.add(self._reason("gh issue close 8 --repo vitalharmony/hrse"))
        self.assertEqual(len(seen), 4, seen)


class TtlTests(unittest.TestCase):
    def test_the_default_ttl_suits_an_unattended_run(self):
        """2.0 was a supervised-run TTL on a feature whose reason for existing
        is running while nobody is watching; the operator stepped away 'a few
        hours' and even a correct authorization would have expired mid-run."""
        self.assertGreaterEqual(ba.DEFAULT_TTL_HOURS, 8.0)


class DerivationTests(StateFixture):
    """AC5 (harmonic-forge#552, carried from #549): resolve a PR to its issue
    from the head branch and title when no `link_pr` record exists.

    Every fail-closed direction gets its own test, because the one way this
    feature could do harm is by widening authorization rather than by failing
    to help.
    """

    def setUp(self):
        super().setUp()
        ba.authorize(["F552"], state_path=self.state_path)
        self.command = ("gh pr merge 561 --repo vitalharmony/harmonic-forge "
                        "--squash")

    def decide(self):
        return ba.decide(self.command, state_path=self.state_path)

    def test_branch_and_title_agreeing_resolves_without_link_pr(self):
        self.carriers = ("l1/f552-consumption-correctness",
                         "fix(batch): consumption correctness (harmonic-forge#552)")
        self.assertEqual(self.decide()[0], "allow")

    def test_branch_alone_is_enough(self):
        """`feat/1754-...` — no prefix letter, so the PR's own repo supplies it."""
        ba.authorize(["H1754"], state_path=self.state_path)
        self.carriers = ("feat/1754-prompt-cache-parity", "no issue reference here")
        self.assertEqual(
            ba.decide("gh pr merge 1758 --repo vitalharmony/hrse --squash",
                      state_path=self.state_path)[0], "allow")

    def test_a_cross_repo_title_reference_wins_over_the_prs_own_repo(self):
        """An hrse PR whose title names `(harmonic-forge#552)` derives F552.

        Real shape: hrse#1752's title is `... (harmonic-forge#521) (#1752)`.
        Reading the number without its slug would have produced H552 — an
        authorization for a different issue in a different repo.
        """
        self.carriers = ("l2/f552-hrse-wiring",
                         "feat(batch): hrse wiring (harmonic-forge#552)")
        self.assertEqual(
            ba.decide("gh pr merge 1799 --repo vitalharmony/hrse --squash",
                      state_path=self.state_path)[0], "allow")

    def test_neither_carrier_parses_asks(self):
        self.carriers = ("scratch", "a title with no issue reference")
        self.assertEqual(self.decide()[0], "ask")

    def test_the_pr_could_not_be_read_asks(self):
        self.carriers = None
        self.assertEqual(self.decide()[0], "ask")

    def test_carriers_disagreeing_asks(self):
        """Disagreement is not a tie to be broken — it is evidence that at
        least one reading is wrong, and picking either would be a guess."""
        self.carriers = ("l1/f552-consumption-correctness",
                         "fix(batch): something else (harmonic-forge#549)")
        self.assertEqual(self.decide()[0], "ask")

    def test_a_derived_key_with_no_authorization_asks(self):
        """Derivation resolves the mapping; it does not create the grant."""
        self.carriers = ("l1/f999-never-authorized",
                         "chore: unrelated (harmonic-forge#999)")
        self.assertEqual(self.decide()[0], "ask")

    def test_a_derived_key_whose_grant_expired_asks(self):
        state = ba._load(self.state_path)
        state["F552"]["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        ba._save(state, self.state_path)
        self.carriers = ("l1/f552-consumption-correctness",
                         "fix(batch): consumption correctness (harmonic-forge#552)")
        self.assertEqual(self.decide()[0], "ask")

    def test_an_unknown_prefix_letter_does_not_fall_back_to_the_prs_repo(self):
        """`z552` names no repo this module knows. Falling back to the PR's own
        repo would be the single way derivation could widen authorization."""
        self.carriers = ("l1/z552-unknown-prefix", "chore: unknown prefix")
        self.assertEqual(self.decide()[0], "ask")

    def test_the_prs_own_number_is_not_a_candidate(self):
        """`gh` appends `(#561)` to a squashed title. A PR is not an
        authorization for itself, and leaving it in would make every squashed
        title disagree with its own branch."""
        self.carriers = ("l1/f552-consumption-correctness",
                         "fix(batch): consumption correctness "
                         "(harmonic-forge#552) (#561)")
        self.assertEqual(self.decide()[0], "allow")

    def test_a_lane_segment_is_not_read_as_an_issue_number(self):
        """`l1/` and `l2/` prefix nearly every branch here. Reading `l1` as
        issue 1 would attach a grant to whatever `H1`/`F1` happened to hold."""
        self.assertEqual(
            ba._BRANCH_ISSUE.findall("l1/f552-consumption-correctness"),
            [("f", "552")])

    def test_link_pr_still_overrides_derivation(self):
        """AC5: `link_pr` remains the explicit record. A PR linked to one key
        must not be re-resolved to another by its branch name."""
        ba.authorize(["F549"], state_path=self.state_path)
        ba.link_pr("F549", "vitalharmony/harmonic-forge", 561,
                   state_path=self.state_path)
        self.carriers = ("l1/f552-consumption-correctness",
                         "fix(batch): consumption correctness (harmonic-forge#552)")
        self.assertEqual(self.decide()[0], "allow")
        consume_ok(self.command, self.state_path)
        state = ba._load(self.state_path)
        self.assertTrue(any(t.get("consumed") for t in state["F549"]["targets"]))
        self.assertFalse(any(t.get("consumed") for t in state["F552"]["targets"]))

    def test_a_derived_merge_is_consumable_and_records_the_mapping(self):
        """The slot carries no `pr_number` on this path. Reading it from the
        slot rather than the command would have left every derived merge
        permanently unconsumed."""
        self.carriers = ("l1/f552-consumption-correctness",
                         "fix(batch): consumption correctness (harmonic-forge#552)")
        self.assertEqual(consume_ok(self.command, self.state_path), ["F552"])
        merge = next(t for t in ba._load(self.state_path)["F552"]["targets"]
                     if t["action"] == "gh pr merge")
        self.assertTrue(merge["consumed"])
        self.assertEqual((merge["repo"], merge["pr_number"]),
                         ("vitalharmony/harmonic-forge", 561))


class CarrierCacheTests(unittest.TestCase):
    """The derivation lookup runs inside a PreToolUse hook, so it is cached.

    Every hook invocation is its own process, which is why the cache is on
    disk: an in-process one would never record a hit. These tests drive the
    REAL `_pr_carriers` (the StateFixture stub is deliberately not used) with
    `subprocess.run` faked, so what is under test is the caching, not `gh`.
    """

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.tmpdir.name) / "pr-carriers.json"
        self.calls = []

    def tearDown(self):
        self.tmpdir.cleanup()

    def _run(self, returncode=0, stdout="l1/f552-x\nfix: x (harmonic-forge#552)\n"):
        def fake(args, **kwargs):
            self.calls.append(args)
            return type("R", (), {"returncode": returncode, "stdout": stdout})()
        return fake

    def test_a_second_lookup_does_not_re_hit_github(self):
        with mock.patch.object(ba.subprocess, "run", self._run()):
            first = ba._pr_carriers("vitalharmony/harmonic-forge", 561,
                                    cache_path=self.cache_path)
            second = ba._pr_carriers("vitalharmony/harmonic-forge", 561,
                                     cache_path=self.cache_path)
        self.assertEqual(first, ("l1/f552-x", "fix: x (harmonic-forge#552)"))
        self.assertEqual(second, first)
        self.assertEqual(len(self.calls), 1)

    def test_the_lookup_is_rest_not_graphql(self):
        """R-0083: `gh api repos/.../pulls/N` is REST. `gh pr view` answers the
        same question through GraphQL and is deliberately not used."""
        with mock.patch.object(ba.subprocess, "run", self._run()):
            ba._pr_carriers("vitalharmony/harmonic-forge", 561,
                            cache_path=self.cache_path)
        self.assertEqual(self.calls[0][:3], ["gh", "api",
                                             "repos/vitalharmony/harmonic-forge/pulls/561"])

    def test_a_different_pr_is_a_separate_entry(self):
        with mock.patch.object(ba.subprocess, "run", self._run()):
            ba._pr_carriers("vitalharmony/harmonic-forge", 561,
                            cache_path=self.cache_path)
            ba._pr_carriers("vitalharmony/harmonic-forge", 562,
                            cache_path=self.cache_path)
        self.assertEqual(len(self.calls), 2)

    def test_a_stale_entry_is_re_fetched(self):
        with mock.patch.object(ba.subprocess, "run", self._run()):
            ba._pr_carriers("vitalharmony/harmonic-forge", 561,
                            cache_path=self.cache_path)
            cache = json.loads(self.cache_path.read_text())
            cache["vitalharmony/harmonic-forge#561"]["at"] = (
                time.time() - ba.CARRIER_CACHE_TTL_SECONDS - 1)
            self.cache_path.write_text(json.dumps(cache))
            ba._pr_carriers("vitalharmony/harmonic-forge", 561,
                            cache_path=self.cache_path)
        self.assertEqual(len(self.calls), 2)

    def test_a_failed_lookup_is_not_cached(self):
        """Caching "unreadable" would turn one transient network failure into
        six hours of prompts."""
        with mock.patch.object(ba.subprocess, "run", self._run(returncode=1)):
            self.assertIsNone(ba._pr_carriers("vitalharmony/harmonic-forge", 561,
                                              cache_path=self.cache_path))
        self.assertFalse(self.cache_path.exists())

    def test_a_corrupt_cache_file_is_survived_not_raised(self):
        self.cache_path.write_text("{not json")
        with mock.patch.object(ba.subprocess, "run", self._run()):
            self.assertIsNotNone(ba._pr_carriers("vitalharmony/harmonic-forge", 561,
                                                 cache_path=self.cache_path))

    def test_the_cache_is_bounded(self):
        with mock.patch.object(ba.subprocess, "run", self._run()):
            for n in range(ba.CARRIER_CACHE_MAX + 25):
                ba._pr_carriers("vitalharmony/harmonic-forge", n,
                                cache_path=self.cache_path)
        self.assertLessEqual(len(json.loads(self.cache_path.read_text())),
                             ba.CARRIER_CACHE_MAX)

    def test_the_cache_path_follows_a_patched_state_path(self):
        """A path bound at import time would silently write the real cache
        from inside a test — the trap `_load`'s docstring names."""
        original = ba.STATE_PATH
        try:
            ba.STATE_PATH = Path(self.tmpdir.name) / "state.json"
            with mock.patch.object(ba.subprocess, "run", self._run()):
                ba._pr_carriers("vitalharmony/harmonic-forge", 561)
            self.assertTrue((Path(self.tmpdir.name) / ba.CARRIER_CACHE_NAME).exists())
        finally:
            ba.STATE_PATH = original


class PrecloseRegressionTests(StateFixture):
    """The defects the harmonic-forge#552 preclose panel found, each with the
    test whose absence let 90 green tests miss it.

    Every one of these was invisible to the suite as it stood: the panel's own
    finding, repeatedly, was not "this code is wrong" but "no test could tell."
    """

    def test_every_authorized_segment_of_a_bundled_command_is_consumed(self):
        """`merge A && merge B` is the normal shape of a BATCH run.

        `consume()` returned on the first success, so B landed while its slot
        still said the merge had never happened — a live grant any later
        command could spend, and (because `block_batch_stop.py` keys on the
        close target) a wedge on every turn-end for the rest of the 12h TTL.
        """
        ba.authorize(["F273", "F274"], state_path=self.state_path)
        ba.link_pr("F273", "vitalharmony/harmonic-forge", 273,
                   state_path=self.state_path)
        ba.link_pr("F274", "vitalharmony/harmonic-forge", 274,
                   state_path=self.state_path)
        command = ("gh pr merge 273 --repo vitalharmony/harmonic-forge --squash "
                   "&& gh pr merge 274 --repo vitalharmony/harmonic-forge --squash")
        self.assertEqual(sorted(consume_ok(command, self.state_path)),
                         ["F273", "F274"])
        state = ba._load(self.state_path)
        for key in ("F273", "F274"):
            merge = next(s for s in state[key]["targets"]
                         if s["action"] == "gh pr merge")
            self.assertTrue(merge["consumed"], f"{key} merged but not consumed")

    def test_a_merge_and_a_close_in_one_call_consume_both_targets(self):
        ba.authorize(["F552"], state_path=self.state_path)
        ba.link_pr("F552", "vitalharmony/harmonic-forge", 561,
                   state_path=self.state_path)
        command = ("gh pr merge 561 --repo vitalharmony/harmonic-forge --squash "
                   "&& gh issue close 552 --repo vitalharmony/harmonic-forge")
        self.assertEqual(consume_ok(command, self.state_path), ["F552", "F552"])
        state = ba._load(self.state_path)
        self.assertTrue(all(s["consumed"] for s in state["F552"]["targets"]))

    def test_consume_takes_no_lock_for_an_unrelated_command(self):
        """`consume()` runs on EVERY Bash tool call. Taking the exclusive lock
        before classifying made every `ls` a contender for the 0.4s budget the
        real merges share."""
        lock_path = self.state_path.with_name(self.state_path.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        holder = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(holder, fcntl.LOCK_EX)
        try:
            start = time.monotonic()
            self.assertEqual(consume_ok("ls -la", self.state_path), [])
            self.assertLess(time.monotonic() - start, 0.2)
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            os.close(holder)

    def test_the_carrier_lookup_asks_for_the_field_rest_actually_returns(self):
        """`.headRefName` is the GraphQL / `gh pr view --json` spelling. Against
        this REST endpoint it resolves to null, and because the title still
        occupied line 2 the function returned `("", title)` rather than failing
        — so derivation was silently TITLE-ONLY and the carriers-disagree guard
        could never fire live. 90 tests were green over it, because the one test
        touching the real function asserted only `argv[:3]`.
        """
        calls = []

        def fake(args, **kwargs):
            calls.append(args)
            return type("R", (), {"returncode": 0, "stdout": "b\nt\n"})()

        # `self._real_carriers`, not `ba._pr_carriers` — StateFixture stubs the
        # latter, and driving the stub is exactly how the wrong field name
        # stayed invisible.
        with mock.patch.object(ba.subprocess, "run", fake):
            self._real_carriers("vitalharmony/hrse", 1753,
                                cache_path=self.state_path.with_name("c.json"))
        self.assertIn("--jq", calls[0])
        jq = calls[0][calls[0].index("--jq") + 1]
        self.assertIn(".head.ref", jq)
        self.assertNotIn("headRefName", jq)

    def test_a_title_that_merely_cites_an_issue_does_not_fulfil_it(self):
        """`fix(hooks): supersedes #549` on a branch with no number derived
        F549 and merged an unauthorized PR against F549's grant."""
        ba.authorize(["F549"], state_path=self.state_path)
        self.carriers = ("fix/batch-auth", "fix(hooks): supersedes #549")
        self.assertEqual(
            ba.decide("gh pr merge 900 --repo vitalharmony/harmonic-forge --squash",
                      state_path=self.state_path)[0], "ask")

    def test_the_documented_trailing_parenthetical_still_resolves(self):
        """The positive control: tightening the regex must not break the shape
        every PR in this house actually uses."""
        ba.authorize(["F544"], state_path=self.state_path)
        self.carriers = ("l1/f544-platform-skills-manifest",
                         "feat(platform): declare the platform skills HRSE2 "
                         "consumes, in a tracked manifest (harmonic-forge#544)")
        self.assertEqual(
            ba.decide("gh pr merge 1753 --repo vitalharmony/hrse --squash",
                      state_path=self.state_path)[0], "allow")

    def test_derivation_never_crosses_the_account_boundary(self):
        """REPO_PREFIXES is a credential-isolation boundary, not a shorthand
        table. A PR in an unmapped repo on another account, branch
        `l2/h395-port`, derived H395 and merged with no prompt."""
        ba.authorize(["H395"], state_path=self.state_path)
        self.carriers = ("l2/h395-port", "chore: port the fix (hrse#395)")
        self.assertEqual(
            ba.decide("gh pr merge 7 --repo kenekted/mve --squash",
                      state_path=self.state_path)[0], "ask")

    def test_a_corrected_title_takes_effect_within_the_cache_ttl(self):
        """Editing the title is the only remedy an author has when their PR
        derives to the wrong issue. A 6h TTL ignored the correction for 6h."""
        self.assertLessEqual(ba.CARRIER_CACHE_TTL_SECONDS, 900)


class RegistrationScopeTests(unittest.TestCase):
    """The gate and its consumer must share a scope (harmonic-forge#552
    preclose, refuters A/B/C — all three found it independently).

    `batch_gate.py` is registered in the USER settings, so `decide()` runs in
    every project. Registering the consumer per-project meant cymagraph-infra
    and openclaw-projects — both BATCH-eligible — gated but never consumed.
    """

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tmpdir.name) / "settings.json"

    def tearDown(self):
        self.tmpdir.cleanup()

    def _write(self, pre, post):
        self.path.write_text(json.dumps({"hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks":
                            [{"command": f"python3 {c}"} for c in pre]}],
            "PostToolUse": [{"matcher": "Bash", "hooks":
                             [{"command": f"python3 {c}"} for c in post]}],
        }}))

    def test_both_registered_is_ok(self):
        self._write(["batch_gate.py"], ["batch_consume.py"])
        self.assertTrue(ba.verify_registration(self.path)[0])

    def test_neither_registered_is_ok(self):
        self._write(["other.py"], ["other.py"])
        self.assertTrue(ba.verify_registration(self.path)[0])

    def test_a_gate_with_no_consumer_is_drift(self):
        """The shipped state before this fix. A merge is allowed and never
        marked spent, so a single-use close target becomes multi-use."""
        self._write(["batch_gate.py"], ["other.py"])
        ok, message = ba.verify_registration(self.path)
        self.assertFalse(ok)
        self.assertIn("batch_consume.py", message)

    def test_a_consumer_with_no_gate_is_drift(self):
        self._write(["other.py"], ["batch_consume.py"])
        ok, message = ba.verify_registration(self.path)
        self.assertFalse(ok)
        self.assertIn("batch_gate.py", message)

    def test_an_unreadable_settings_file_is_drift_not_a_pass(self):
        ok, _ = ba.verify_registration(self.path / "nope.json")
        self.assertFalse(ok)

    def test_the_live_user_settings_are_consistent(self):
        """The distribution step itself, asserted rather than described
        (R-0353: the authoring repo is not the shipping surface)."""
        ok, message = ba.verify_registration()
        self.assertTrue(ok, message)
