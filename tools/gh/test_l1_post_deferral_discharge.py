#!/usr/bin/env python3
"""harmonic-forge#1564 AC15 (design amendment): a posted handoff discharges
the issue's recorded R-0352 deferral at the writer, with no thread read, and a
`--dry-run` discharges nothing.

The post case drives `l1_post.post_kind(... "handoff" ...)`, the path
`main()` takes for a live handoff, with only the GitHub transport and the
world checks mocked. The dry-run case drives `main()` itself through the
dry-run harness, with the real `post_kind` in place."""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import deferral_store  # noqa: E402
import l1_post as L  # noqa: E402
from test_l1_post_dry_run import Harness, R4L3, REPO, SHA_A  # noqa: E402

N = 2764
HANDOFF = ("## Handoff: [Issue #2764 — test]\n\n**Scope:** s.\n**Next:** n.\n"
           "**Threat model:** good-faith\n\n### Affected Files\n| File | Lines | Change type |\n"
           "|---|---|---|\n| tools/gh/x.py | 1 | Modify |\n\n### Root Cause / Entry Point\n> x\n")


class _Store:
    def _store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(deferral_store, "DEFAULT_CANDIDATES_DIR", Path(tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        deferral_store.record_deferral(REPO, N, "filing", operator_login="vitalharmony")
        self.assertIsNotNone(deferral_store.deferral_entry(REPO, N))


class HandoffPostDischargesTheDeferral(_Store, unittest.TestCase):
    def setUp(self):
        self._store()

    def _post(self, kind="handoff", body=HANDOFF, receipt=None, **kw):
        def no_thread_read(*args, **_kwargs):
            raise AssertionError(f"a thread read on the post path: {args}")

        self.out = io.StringIO()
        patches = [
            mock.patch.object(L, "world_checks", return_value=(["issue-open"], [])),
            mock.patch.object(L, "validate_sticky_wicket"),
            mock.patch.object(L, "write_receipt", side_effect=receipt),
            mock.patch.object(L, "_discharge_handoff_owed"),
            mock.patch.object(L, "_record_attrib"),
            mock.patch.object(L, "_emit_attempt"),
            mock.patch.object(L, "record_overlap_rulings"),
            mock.patch.object(L, "_afk_toggle", None),
            mock.patch.object(L, "_tasklist_owed", None),
            mock.patch.object(L.pitch_receipt, "consume"),
            mock.patch.object(L._route_snapshot, "record_snapshot", return_value=None),
            mock.patch.object(L, "comment_body", return_value=("https://example.test/c", 7)),
            mock.patch.object(L, "check_lane3_ready_fetch_comments", side_effect=no_thread_read),
            mock.patch.object(L.subprocess, "run", side_effect=no_thread_read),
        ]
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(contextlib.redirect_stdout(self.out))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            return L.post_kind(REPO, N, kind, body, SHA_A, "main", **kw)

    def test_a_posted_handoff_removes_the_entry(self):
        self.assertEqual(self._post(plan_first=False, labels=set()), ("https://example.test/c", 7))
        self.assertIsNone(deferral_store.deferral_entry(REPO, N))

    def test_another_kind_leaves_the_entry(self):
        self._post(kind="rework", body="## Rework — F2764\n\n**Finding:** x\n**Next:** n\n",
                   rework_kind="rebase")
        self.assertIsNotNone(deferral_store.deferral_entry(REPO, N))

    def test_a_store_error_never_fails_the_post(self):
        """The discharge is really called, raises, and the post still returns,
        with the one printed line (preclose #12: without the call this test
        passed too)."""
        with mock.patch.object(deferral_store, "discharge_deferral",
                               side_effect=RuntimeError("x")) as discharge:
            self.assertEqual(self._post(plan_first=False, labels=set()), ("https://example.test/c", 7))
        discharge.assert_called_once_with(REPO, N, reason="handoff posted")
        self.assertIn("[l1-post] deferral not discharged: RuntimeError: x", self.out.getvalue())

    def test_a_receipt_failure_after_the_post_still_discharges(self):
        """preclose #11: the discharge runs right after the confirmed post,
        before the fallible local receipt write."""
        with self.assertRaises(OSError):
            self._post(plan_first=False, labels=set(), receipt=OSError("disk full"))
        self.assertIsNone(deferral_store.deferral_entry(REPO, N))


class DryRunDischargesNothing(_Store, Harness):
    def setUp(self):
        super().setUp()
        self._store()

    def test_a_handoff_dry_run_leaves_the_entry(self):
        quiet = ["validate_handoff", "validate_plan_first_declared", "validate_plan_first_spec",
                 "validate_board_fields_set", "validate_milestone_set",
                 "validate_tooling_exception_labelled", "validate_pitch_inspected",
                 "validate_te_lead_fields", "validate_ui_brand", "validate_lead"]
        with contextlib.ExitStack() as stack:
            for name in quiet:
                stack.enter_context(mock.patch.object(L, name))
            code, _, err = self.run_main("--repo", REPO, "--issue", N, "--kind", "handoff",
                                         "--sha", SHA_A, "--branch", "main",
                                         "--file", self.body(R4L3), "--plan-first", "false",
                                         "--dry-run", real_post_kind=True)
        self.assertIsNone(code, err)
        self.assertIsNotNone(deferral_store.deferral_entry(REPO, N))


if __name__ == "__main__":
    unittest.main()
