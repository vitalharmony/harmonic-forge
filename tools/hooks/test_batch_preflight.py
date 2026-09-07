#!/usr/bin/env python3
"""Tests for `batch_preflight.py` (harmonic-forge#509 AC1/AC2).

Hermetic — `gh` is patched throughout. `#500`'s tests read operator machine
state and turned CI red on `main` (fixed in #505), and `#502`'s wrote to the
operator's live authorization store. Neither happens here: nothing in this file
shells out, and nothing touches the real state file.
"""
from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import batch_preflight as bp  # noqa: E402


def issue(labels: list[str], state: str = "OPEN") -> str:
    return json.dumps({"state": state,
                       "labels": [{"name": n} for n in labels]})


class ResolveRepoTests(unittest.TestCase):
    def test_a_known_prefix_resolves(self) -> None:
        self.assertEqual(bp.resolve_repo("H1631"), ("vitalharmony/hrse", "1631"))
        self.assertEqual(bp.resolve_repo("F509"),
                         ("vitalharmony/harmonic-forge", "509"))

    def test_case_is_ignored(self) -> None:
        self.assertEqual(bp.resolve_repo("h1631"), ("vitalharmony/hrse", "1631"))

    def test_an_unmapped_prefix_is_none(self) -> None:
        self.assertIsNone(bp.resolve_repo("Q4"))

    def test_a_malformed_key_is_none(self) -> None:
        for key in ("", "1631", "HH1631", "H", "H-1631"):
            with self.subTest(key=key):
                self.assertIsNone(bp.resolve_repo(key))


class PrecloseCheckTests(unittest.TestCase):
    """This guard is the expensive one: it fires on EACH close, so a batch of
    N tooling issues halts N times. That is what makes unattended tooling
    batches structurally impossible today."""

    def _check(self, payload: str | None) -> bp.Finding:
        with mock.patch.object(bp, "_gh", return_value=payload):
            return bp.check_preclose("F509")

    def test_armed_and_unsatisfied_is_unmet(self) -> None:
        f = self._check(issue(["tooling-exception", "bug"]))
        self.assertEqual(f.status, bp.UNMET)
        self.assertIn("WILL halt", f.detail)
        self.assertIn("--add-label preclose-inspected", f.detail)

    def test_armed_and_satisfied_is_ok(self) -> None:
        f = self._check(issue(["tooling-exception", "preclose-inspected"]))
        self.assertEqual(f.status, bp.OK)

    def test_not_armed_is_ok(self) -> None:
        """No `tooling-exception` label means the guard never arms."""
        f = self._check(issue(["bug"]))
        self.assertEqual(f.status, bp.OK)
        self.assertIn("does not arm", f.detail)

    def test_an_already_closed_issue_is_ok(self) -> None:
        f = self._check(issue(["tooling-exception"], state="CLOSED"))
        self.assertEqual(f.status, bp.OK)

    def test_an_unreadable_issue_is_unknown_not_ok(self) -> None:
        """Unknown must never read as clear — that is how a preflight becomes
        a check that always passes."""
        self.assertEqual(self._check(None).status, bp.UNKNOWN)

    def test_unparseable_json_is_unknown(self) -> None:
        self.assertEqual(self._check("not json").status, bp.UNKNOWN)

    def test_an_unmapped_key_is_unknown(self) -> None:
        with mock.patch.object(bp, "_gh", return_value=issue([])):
            self.assertEqual(bp.check_preclose("Q4").status, bp.UNKNOWN)

    def test_it_mirrors_the_guard_rather_than_being_stricter(self) -> None:
        """The guard checks label presence only, not whether a lane actually
        reviewed anything. A preflight stricter than the guard would report
        halts that will not happen."""
        source = (HERE / "block_missing_preclose_inspection.py").read_text(
            encoding="utf-8")
        self.assertIn("preclose-inspected", source)
        self.assertIn("tooling-exception", source)


class AuthorizationCheckTests(unittest.TestCase):
    def test_a_live_key_is_ok(self) -> None:
        with mock.patch("batch_context.live_batch_keys", return_value=["F509"]):
            self.assertEqual(bp.check_authorization("F509").status, bp.OK)

    def test_a_missing_key_is_unmet_and_says_how_to_fix_it(self) -> None:
        with mock.patch("batch_context.live_batch_keys", return_value=[]):
            f = bp.check_authorization("H1631")
        self.assertEqual(f.status, bp.UNMET)
        self.assertIn("BATCH H1631", f.detail)

    def test_case_is_ignored(self) -> None:
        with mock.patch("batch_context.live_batch_keys", return_value=["f509"]):
            self.assertEqual(bp.check_authorization("F509").status, bp.OK)

    def test_an_unreadable_state_is_unknown(self) -> None:
        with mock.patch("batch_context.live_batch_keys",
                        side_effect=RuntimeError):
            self.assertEqual(bp.check_authorization("F509").status, bp.UNKNOWN)


class ReportTests(unittest.TestCase):
    def _report(self, preclose: bp.Finding, live: list[str]) -> tuple[str, int]:
        with mock.patch.object(bp, "check_preclose", return_value=preclose), \
                mock.patch("batch_context.live_batch_keys", return_value=live):
            return bp.report(["F509"])

    def test_all_clear_exits_zero(self) -> None:
        text, code = self._report(bp.Finding("F509", "preclose", bp.OK, "fine"),
                                  ["F509"])
        self.assertEqual(code, 0)
        self.assertIn("Nothing known will halt", text)

    def test_an_unmet_row_exits_one(self) -> None:
        text, code = self._report(
            bp.Finding("F509", "preclose", bp.UNMET, "will halt"), ["F509"])
        self.assertEqual(code, 1)
        self.assertIn("WILL halt", text)

    def test_unknown_alone_exits_zero_but_says_it_is_unverified(self) -> None:
        """Unknown is not a finding, and must not read as a clean bill."""
        text, code = self._report(
            bp.Finding("F509", "preclose", bp.UNKNOWN, "could not read"), ["F509"])
        self.assertEqual(code, 0)
        self.assertIn("unverified", text)

    def test_the_report_names_the_guards_it_cannot_pre_satisfy(self) -> None:
        """Silence about them would read as 'nothing else can stop you'."""
        text, _ = self._report(bp.Finding("F509", "preclose", bp.OK, "fine"),
                               ["F509"])
        for name, _why in bp.COMPOSITION_GUARDS:
            self.assertIn(name, text)

    def test_it_does_not_auto_satisfy_preclose(self) -> None:
        """AC1 is explicit: it REPORTS, it does not run preclose and tick the
        box — that would forge a review.

        Asserted on `_gh`'s actual calls, not on source text. The first version
        greped a region that structurally could not contain the string, and
        greped a second literal with exact spacing: an auto-satisfy call written
        as `_gh("issue","edit", ...)` passed all 45 tests while the preflight
        forged the review AC1 forbids.
        """
        calls: list[tuple] = []

        def record(*args: str) -> str | None:
            calls.append(args)
            return issue(["tooling-exception"])

        with mock.patch.object(bp, "_gh", side_effect=record):
            bp.report(["F509"])

        self.assertTrue(calls, "the preflight made no gh call at all")
        for call in calls:
            self.assertNotIn("edit", call, f"mutating gh call: {call}")
            self.assertNotIn("--add-label", call, f"mutating gh call: {call}")
            self.assertEqual(call[0], "issue")
            self.assertEqual(call[1], "view", f"non-read gh verb: {call}")

    def test_every_gh_call_the_preflight_makes_is_a_read(self) -> None:
        """The general form of the above: no mutating verb, ever, for any key."""
        calls: list[tuple] = []
        with mock.patch.object(bp, "_gh",
                               side_effect=lambda *a: (calls.append(a),
                                                       issue([]))[1]):
            bp.report(["F509", "H1631"])
        mutating = {"edit", "create", "close", "comment", "delete", "merge"}
        for call in calls:
            self.assertFalse(mutating & set(call), f"mutating gh call: {call}")


class ExitCodeTests(unittest.TestCase):
    """0 green, 1 a finding, 2 could not run — a tool returning non-zero for
    both leaves a caller unable to tell a finding from misconfiguration."""

    def _main(self, argv: list[str], **patches) -> int:
        with mock.patch.object(sys, "stdout", io.StringIO()), \
                mock.patch.object(sys, "stderr", io.StringIO()):
            for target, kw in patches.items():
                mock.patch.object(bp, target, **kw).start()
            try:
                return bp.main(argv)
            finally:
                mock.patch.stopall()

    def test_no_keys_exits_two(self) -> None:
        self.assertEqual(self._main([]), 2)

    def test_from_state_with_an_unreadable_store_exits_two(self) -> None:
        with mock.patch("batch_context.live_batch_keys", side_effect=RuntimeError), \
                mock.patch.object(sys, "stdout", io.StringIO()), \
                mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(bp.main(["--from-state"]), 2)

    def test_a_finding_exits_one(self) -> None:
        code = self._main(["--key", "F509"],
                          report={"return_value": ("x", 1)})
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
