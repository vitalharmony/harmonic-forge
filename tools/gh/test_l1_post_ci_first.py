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
        with mock.patch.object(L.gate_ci, "ci_conclusion", return_value=(state, "verify: failure")), \
             mock.patch.object(L, "static_checks", side_effect=self._check), \
             contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            L.post_kind(REPO, 5, "ready-for-l3", BODY, SHA, "br")
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


if __name__ == "__main__":
    unittest.main()
