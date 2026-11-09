#!/usr/bin/env python3
"""harmonic-forge#1016 -- `ready-for-l3` reads the attested SHA's required
`verify` conclusion before the full local check. Red refuses in seconds and
records one `refused-ci` attempt; every other state proceeds unchanged."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import l1_post as L  # noqa: E402

REPO = "vitalharmony/harmonic-forge"
SHA = "b" * 40
BODY = "## ready-for-l3 — F5\n\n**Target:** x\n**Verified:** y\n**Next:** z\n"


class CiFirstTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name) / "store"
        for patcher in (
            mock.patch.dict(os.environ, {"HARMONIC_FORGE_TELEMETRY_STORE": str(self.store)}),
            mock.patch.object(L, "world_checks", return_value=([], [])),
            mock.patch.object(L, "refresh_main", return_value="f" * 40),
            mock.patch.object(L, "validate_sticky_wicket"),
            mock.patch.object(L.orphan_report, "report"),
            mock.patch.object(L, "_cwd_repo_from_git", return_value="o/checkout"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.check_calls: list = []

    def _check(self, sha, branch):
        self.check_calls.append(sha)
        # Stop right after the check, so the test never reaches the post.
        L.fail("stop after static_checks")

    def _run(self, state: str) -> str:
        err = io.StringIO()
        with mock.patch.object(L.gate_ci, "ci_conclusion", return_value=(state, "verify: failure")), \
             mock.patch.object(L, "static_checks", side_effect=self._check), \
             contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            L.post_kind(REPO, 5, "ready-for-l3", BODY, SHA, "br")
        self.stderr = err.getvalue()
        return str(ctx.exception.code or "")

    def events(self) -> list[dict]:
        return [json.loads(line) for part in self.store.glob("events/**/*.jsonl")
                for line in part.read_text().splitlines() if line.strip()]

    def test_red_refuses_before_static_checks(self):
        refusal = self._run("red")
        self.assertEqual(self.check_calls, [], "static_checks must not run on a red CI")
        self.assertIn("is red (verify: failure)", refusal)
        self.assertIn("full local check was not started", refusal)

    def test_pending_proceeds(self):
        refusal = self._run("pending")
        self.assertEqual(self.check_calls, [SHA])
        self.assertIn("stop after static_checks", refusal)

    def test_other_states_proceed(self):
        for state in ("green", "absent", "unknown"):
            self.check_calls.clear()
            self._run(state)
            self.assertEqual(self.check_calls, [SHA], state)

    def test_red_refusal_emits_one_event(self):
        # Spy on the emitter: the store dedupes same-second events, so a
        # second (refused-other) emit would be invisible in events() alone.
        with mock.patch.object(L, "_emit_attempt", wraps=L._emit_attempt) as spy:
            self._run("red")
        self.assertEqual([c.args[3] for c in spy.call_args_list], ["refused-ci"])
        events = self.events()
        self.assertEqual(len(events), 1, events)
        attrs = events[0]["attrs"]
        self.assertEqual((attrs["outcome"], attrs["ci_state"], attrs["local_check_result"]),
                         ("refused-ci", "red", "not-run"))

    def test_a_ci_read_error_fails_open(self):
        with mock.patch.object(L.gate_ci, "ci_conclusion", side_effect=RuntimeError("gh down")), \
             mock.patch.object(L, "static_checks", side_effect=self._check), \
             contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            L.post_kind(REPO, 5, "ready-for-l3", BODY, SHA, "br")
        self.assertEqual(self.check_calls, [SHA])

    def test_no_source_repo_fails_open(self):
        with mock.patch.object(L, "_cwd_repo_from_git", return_value=None):
            self._run("red")
        self.assertEqual(self.check_calls, [SHA])

    def test_the_real_post_prints_no_dry_run_note(self):
        """harmonic-forge#1301 AC2: the real post's call keeps the default."""
        for state in ("pending", "absent"):
            self._run(state)
            self.assertNotIn("has not concluded", self.stderr, state)


class DryRunNoteTests(unittest.TestCase):
    """harmonic-forge#1301 AC1/AC3: the dry run's flag, state by state."""

    def _call(self, state, *, side_effect=None, flag=True) -> str:
        err = io.StringIO()
        conclusion = (mock.patch.object(L.gate_ci, "ci_conclusion", side_effect=side_effect) if side_effect
                      else mock.patch.object(L.gate_ci, "ci_conclusion", return_value=(state, "verify: in_progress")))
        with conclusion, mock.patch.object(L, "_cwd_repo_from_git", return_value="o/checkout"), \
             contextlib.redirect_stderr(err):
            L._refuse_if_ci_red(REPO, 5, SHA, None, note_unconcluded=flag)
        return err.getvalue()

    def test_pending_and_absent_print_one_line_and_proceed(self):
        for state in ("pending", "absent"):
            out = self._call(state)
            self.assertIn(f"CI on {SHA[:12]} has not concluded ({state}: verify: in_progress)", out)
            self.assertEqual(out.count("\n"), 1, out)

    def test_green_prints_nothing(self):
        self.assertEqual(self._call("green"), "")

    def test_red_still_refuses(self):
        with self.assertRaises(SystemExit) as ctx:
            self._call("red")
        self.assertIn("is red", str(ctx.exception.code))

    def test_unknown_and_a_read_error_fail_open_silently(self):
        self.assertEqual(self._call("unknown"), "")
        self.assertEqual(self._call(None, side_effect=RuntimeError("gh down")), "")

    def test_the_dry_run_step_passes_the_flag(self):
        """harmonic-forge#1301 pitch finding 1: the `ready-for-l3` dry-run
        step list (not `_refuse_if_ci_red` called directly) prints the note,
        so dropping the flag at the call site fails here."""
        with mock.patch.object(L, "refresh_main"), mock.patch.object(L, "world_checks"), \
             mock.patch.object(L, "require_open_pr"):
            steps = L._publish_checks(REPO, 5, "ready-for-l3", SHA, "br", ack_overlap=None,
                                      ack_no_pr_required=None, grant_on_main=False)
        err = io.StringIO()
        with mock.patch.object(L.gate_ci, "ci_conclusion", return_value=("pending", "verify: queued")), \
             mock.patch.object(L, "_cwd_repo_from_git", return_value="o/checkout"), \
             mock.patch.object(L, "refresh_main"), mock.patch.object(L, "world_checks"), \
             mock.patch.object(L, "require_open_pr"), contextlib.redirect_stderr(err):
            for step in steps:
                step()
        self.assertIn("has not concluded (pending: verify: queued)", err.getvalue())

    def test_without_the_flag_nothing_is_printed(self):
        for state in ("pending", "absent"):
            self.assertEqual(self._call(state, flag=False), "", state)


if __name__ == "__main__":
    unittest.main()
