#!/usr/bin/env python3
"""harmonic-forge#687 — the owed-handoff obligation and its Stop hook.

Written AC4 and AC5 first, per the handoff: they are the two that would
otherwise be missed, and AC4 is the observed failure shape (hrse#1919 —
issue created, board-field write rejected, non-zero exit) rather than a
hypothetical.

Every test redirects `handoff_owed.OWED_DIR` into a temp directory. The
module resolves it at import time into a module-level constant, so it is
patched with `mock.patch.object` exactly as `test_statusline_compaction.py`
does for `MARKER_DIR` — reassigning `Path.home()` would not reach it.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gh"))

import enforce_handoff_owed as hook  # noqa: E402
import handoff_owed  # noqa: E402

REPO = "vitalharmony/harmonic-forge"
GH_ISSUE = Path(__file__).resolve().parent.parent / "gh" / "gh_issue.py"


class _TempStore(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(
            handoff_owed, "OWED_DIR", Path(self._tmp.name) / "handoff_owed")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def block_reason(self, session: str) -> str:
        verdict, reason = hook.decide({"session_id": session})
        return reason if verdict == "block" else ""


class AC4BoardWriteFailure(_TempStore):
    """A run that creates the issue and THEN fails must still owe a handoff."""

    def test_the_record_is_written_before_anything_that_can_fail(self):
        """The ordering IS the acceptance criterion.

        `gh_issue.py` records immediately after `create_issue()` returns a
        URL and before board resolution, so all three post-creation exits
        (no-board-with-requested-fields, `add_to_board()` failure, success)
        are already covered by one placement. This asserts the source
        ordering directly, because a later refactor that moves the record
        below the board writes would pass every other test in this file
        while reintroducing hrse#1919 exactly.
        """
        source = GH_ISSUE.read_text(encoding="utf-8")
        record_at = source.index("handoff_owed.record(")
        board_at = source.index("resolve_board_for_repo(args.repo)")
        self.assertLess(
            record_at, board_at,
            "the obligation must be recorded before board resolution — "
            "hrse#1919 created the issue and then exited non-zero")

    def test_a_nonzero_exit_does_not_discard_the_record(self):
        """Simulates the run: record written, then the process exits 1."""
        handoff_owed.record(REPO, 1919, "https://x/issues/1919", key="s1")
        self.assertEqual(len(handoff_owed.outstanding("s1")), 1)
        self.assertIn("1919", self.block_reason("s1"))

    def test_a_failed_creation_records_nothing(self):
        """No issue means no obligation — a block nobody can discharge is
        the one outcome worse than missing the block entirely."""
        self.assertEqual(handoff_owed.outstanding("s1"), [])
        self.assertEqual(self.block_reason("s1"), "")


class AC5IndependentObligations(_TempStore):
    """Two issues in one turn are two obligations; discharging one keeps
    the other."""

    def setUp(self) -> None:
        super().setUp()
        handoff_owed.record(REPO, 701, "https://x/issues/701", key="s1")
        handoff_owed.record(REPO, 702, "https://x/issues/702", key="s1")

    def test_both_are_recorded(self):
        self.assertEqual(
            sorted(e["issue"] for e in handoff_owed.outstanding("s1")),
            [701, 702])

    def test_discharging_one_still_blocks_on_the_other(self):
        handoff_owed.discharge(REPO, 701)
        reason = self.block_reason("s1")
        self.assertIn("702", reason)
        self.assertNotIn("701", reason)

    def test_discharging_both_allows_the_stop(self):
        handoff_owed.discharge(REPO, 701)
        handoff_owed.discharge(REPO, 702)
        self.assertEqual(self.block_reason("s1"), "")
        self.assertEqual(hook.decide({"session_id": "s1"})[0], "allow")

    def test_the_same_number_in_a_different_repo_is_a_different_obligation(self):
        """`hrse#701` and `harmonic-forge#701` are not the same issue."""
        handoff_owed.record("vitalharmony/hrse", 701, key="s1")
        handoff_owed.discharge(REPO, 701)
        self.assertEqual(
            [(e["repo"], e["issue"]) for e in handoff_owed.outstanding("s1")],
            [(REPO, 702), ("vitalharmony/hrse", 701)])


class AC1Blocking(_TempStore):
    def test_a_turn_with_no_filing_is_never_blocked(self):
        self.assertEqual(hook.decide({"session_id": "quiet"}), ("allow", ""))

    def test_the_message_names_the_issue_and_the_literal_command(self):
        handoff_owed.record(REPO, 687, "https://x/issues/687", key="s1")
        reason = self.block_reason("s1")
        self.assertIn(f"{REPO}#687", reason)
        self.assertIn("mise run l1-post", reason)
        self.assertIn("--kind handoff", reason)
        self.assertIn("--issue 687", reason)

    def test_all_three_discharge_routes_are_named(self):
        """Risk 1 from the accepted plan: `l1_post.py` validates handoff
        shape, so a session whose handoff is rejected must be able to see
        the other two routes at the moment it is stuck."""
        handoff_owed.record(REPO, 687, key="s1")
        reason = self.block_reason("s1")
        self.assertIn("--handoff-exception", reason)
        self.assertIn("stale", reason)

    def test_the_recursion_guard_is_honoured_first(self):
        handoff_owed.record(REPO, 687, key="s1")
        with mock.patch.object(sys, "stdin",
                               _stdin({"session_id": "s1", "stop_hook_active": True})):
            with mock.patch("builtins.print") as printed:
                self.assertEqual(hook.main(), 0)
        printed.assert_not_called()

    def test_a_missing_session_id_never_blocks(self):
        """Without a key there is no way to attribute an obligation to this
        turn, and blocking on someone else's record is worse than missing."""
        handoff_owed.record(REPO, 687, key="s1")
        self.assertEqual(hook.decide({"session_id": ""}), ("allow", ""))
        self.assertEqual(hook.decide({}), ("allow", ""))

    def test_an_unreadable_store_never_blocks(self):
        """Exercised through the real filesystem, not by mocking `_read`.

        The first version of this test patched `_read` to raise `OSError` —
        which cannot happen, because `_read` already catches it. That test
        would have passed against a `decide()` with no defence at all while
        asserting nothing about the code as it actually runs. A store whose
        session file is unreadable is the real shape, so that is what this
        makes.
        """
        import os
        handoff_owed.OWED_DIR.mkdir(parents=True, exist_ok=True)
        unreadable = handoff_owed.OWED_DIR / "s1.json"
        unreadable.write_text(json.dumps([{"repo": REPO, "issue": 687}]),
                              encoding="utf-8")
        os.chmod(unreadable, 0o000)
        self.addCleanup(os.chmod, unreadable, 0o600)
        self.assertEqual(hook.decide({"session_id": "s1"})[0], "allow")

    def test_a_store_that_raises_at_any_depth_still_ends_the_turn(self):
        """`main()` is the production contract: a hook that can wedge a
        session into never ending its turn is worse than one that misses a
        stop. Asserted against a failure injected below every guard."""
        with mock.patch.object(hook, "decide", side_effect=RuntimeError):
            with mock.patch.object(sys, "stdin", _stdin({"session_id": "s1"})):
                with mock.patch("builtins.print") as printed:
                    self.assertEqual(hook.main(), 0)
        printed.assert_not_called()


class AC6PositiveSignalOnly(_TempStore):
    def test_an_issue_created_by_another_route_produces_no_obligation(self):
        """The obligation exists only because `gh_issue.py` wrote it. This
        is the gate-on-presence design `block_missing_preclose_inspection.py`
        was corrected into — gating on absence made an ordinary action
        deniable with a lie as its only escape."""
        self.assertEqual(hook.decide({"session_id": "s1"}), ("allow", ""))


class AC3ExceptionRequiresAReason(unittest.TestCase):
    """The exception is refused at the parser, before anything is filed."""

    def _run(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(GH_ISSUE), "--repo", REPO,
             "--title", "t", "--milestone", "Later", *extra],
            capture_output=True, text=True)

    def test_an_exception_without_a_reason_is_refused(self):
        result = self._run("--handoff-exception", "parent-epic")
        self.assertEqual(result.returncode, 2)
        self.assertIn("requires --handoff-exception-reason", result.stderr)

    def test_an_empty_reason_is_refused(self):
        result = self._run("--handoff-exception", "parent-epic",
                           "--handoff-exception-reason", "   ")
        self.assertEqual(result.returncode, 2)

    def test_a_reason_naming_no_rule_is_refused(self):
        result = self._run("--handoff-exception-reason", "because")
        self.assertEqual(result.returncode, 2)
        self.assertIn("names no rule", result.stderr)

    def test_an_unrecognised_exception_is_refused(self):
        result = self._run("--handoff-exception", "i-am-busy",
                           "--handoff-exception-reason", "r")
        self.assertEqual(result.returncode, 2)


class AC3ExceptionPostsItsReason(_TempStore):
    def setUp(self) -> None:
        super().setUp()
        import gh_issue
        self.gh_issue = gh_issue
        self.args = mock.Mock(repo=REPO, handoff_exception="parent-epic",
                              handoff_exception_reason="children carry the work")
        handoff_owed.record(REPO, 700, key="s1")

    def test_the_reason_is_posted_to_the_issue_then_the_record_clears(self):
        with mock.patch.object(self.gh_issue, "_run",
                               return_value=mock.Mock(returncode=0, stderr="")) as run:
            self.assertTrue(
                self.gh_issue._discharge_exception(self.args, 700, "https://x/700"))
        body = next(a for a in run.call_args[0][0] if a.startswith("body="))
        self.assertIn("children carry the work", body)
        self.assertIn("parent-epic", body)
        self.assertEqual(handoff_owed.outstanding("s1"), [])

    def test_a_failed_comment_leaves_the_obligation_standing(self):
        """AC3 says the reason goes to the ISSUE, never to local state
        alone. So if the comment does not post, the claim did not happen."""
        with mock.patch.object(self.gh_issue, "_run",
                               return_value=mock.Mock(returncode=1, stderr="boom")):
            self.assertFalse(
                self.gh_issue._discharge_exception(self.args, 700, "https://x/700"))
        self.assertEqual(len(handoff_owed.outstanding("s1")), 1)

    def test_no_exception_claimed_is_a_no_op(self):
        self.args.handoff_exception = None
        with mock.patch.object(self.gh_issue, "_run") as run:
            self.assertTrue(
                self.gh_issue._discharge_exception(self.args, 700, "https://x/700"))
        run.assert_not_called()
        self.assertEqual(len(handoff_owed.outstanding("s1")), 1)


class StoreMechanics(_TempStore):
    def test_recording_the_same_issue_twice_keeps_one_entry(self):
        handoff_owed.record(REPO, 687, key="s1")
        handoff_owed.record(REPO, 687, key="s1")
        self.assertEqual(len(handoff_owed.outstanding("s1")), 1)

    def test_discharge_sweeps_every_session_not_just_the_caller(self):
        """A handoff is routinely posted by a different session than the one
        that filed. Session-scoping the clear would leave the filer blocked
        with the handoff already live on the issue."""
        handoff_owed.record(REPO, 687, key="filer")
        self.assertEqual(handoff_owed.discharge(REPO, 687), 1)
        self.assertEqual(handoff_owed.outstanding("filer"), [])

    def test_discharging_something_never_recorded_is_zero_not_an_error(self):
        self.assertEqual(handoff_owed.discharge(REPO, 4242), 0)

    def test_stale_files_are_pruned_on_write(self):
        handoff_owed.record(REPO, 1, key="ancient")
        stale = handoff_owed.OWED_DIR / "ancient.json"
        old = time.time() - (handoff_owed.PRUNE_AFTER_DAYS + 1) * 86400
        import os
        os.utime(stale, (old, old))
        handoff_owed.record(REPO, 2, key="current")
        self.assertFalse(stale.exists())
        self.assertEqual(len(handoff_owed.outstanding("current")), 1)

    def test_a_fresh_file_is_not_pruned(self):
        handoff_owed.record(REPO, 1, key="other")
        handoff_owed.record(REPO, 2, key="current")
        self.assertEqual(len(handoff_owed.outstanding("other")), 1)

    def test_an_unwritable_store_reports_failure_without_raising(self):
        with mock.patch.object(Path, "mkdir", side_effect=OSError):
            self.assertFalse(handoff_owed.record(REPO, 687, key="s1"))

    def test_a_corrupt_session_file_reads_as_empty(self):
        handoff_owed.OWED_DIR.mkdir(parents=True, exist_ok=True)
        (handoff_owed.OWED_DIR / "s1.json").write_text("{not json",
                                                       encoding="utf-8")
        self.assertEqual(handoff_owed.outstanding("s1"), [])


class IssueNumberParsing(unittest.TestCase):
    def test_the_number_comes_out_of_the_url_create_issue_returns(self):
        import gh_issue
        self.assertEqual(
            gh_issue._issue_number("https://github.com/vitalharmony/hrse/issues/1919"),
            1919)

    def test_an_unparseable_url_records_nothing(self):
        import gh_issue
        for bad in ("", "not a url", "https://github.com/o/r/pull/12"):
            self.assertIsNone(gh_issue._issue_number(bad))


def _stdin(payload: dict):
    import io
    return io.StringIO(json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
