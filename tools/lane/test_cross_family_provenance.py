"""harmonic-forge#598 AC2/AC3 preclose finding 1 — the provenance label is
computed from the envelope, not composed by hand."""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent / "cross_family_provenance.py"
_spec = importlib.util.spec_from_file_location("cross_family_provenance", _MODULE_PATH)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

_MODEL = "gpt-5.6-sol"
_OWN = "claude-opus-5"


def _ok(assumptions: list[dict]) -> dict:
    return {"family": "codex", "posture": "verify", "status": "ok", "exit_code": 0,
            "report": {"summary": "s", "findings": [], "assumptions": assumptions}}


def _a(verdict: str) -> dict:
    return {"assumption": "x", "verdict": verdict, "evidence": "out"}


class ClassifyTests(unittest.TestCase):
    def test_a_checked_assumption_earns_the_cross_family_label(self):
        label = m.classify(_ok([_a("confirmed")]), _MODEL, _OWN)
        self.assertIn("cross-family", label)
        self.assertIn(_MODEL, label)

    def test_refuted_counts_as_checked(self):
        self.assertIn("cross-family", m.classify(_ok([_a("refuted")]), _MODEL, _OWN))

    def test_all_uncheckable_does_not_earn_the_cross_family_label(self):
        """The state the prose had no name for: exit 0, `status: ok`, and no
        checking whatsoever."""
        label = m.classify(_ok([_a("uncheckable"), _a("uncheckable")]), _MODEL, _OWN)
        self.assertNotIn("cross-family (", label)
        self.assertIn("in-family fallback", label)
        self.assertIn("checked nothing", label)

    def test_the_all_uncheckable_label_names_the_likely_cause(self):
        label = m.classify(_ok([_a("uncheckable")]), _MODEL, _OWN)
        self.assertIn("--cwd", label)

    def test_a_mixed_result_is_cross_family_and_reports_both_counts(self):
        label = m.classify(_ok([_a("confirmed"), _a("uncheckable")]), _MODEL, _OWN)
        self.assertIn("cross-family", label)
        self.assertIn("1 of 2", label)
        self.assertIn("1 uncheckable", label)

    def test_a_process_error_is_a_fallback_naming_the_status(self):
        envelope = {"family": "codex", "posture": "verify", "status": "process-error",
                    "exit_code": 1, "report": None, "stderr": "codex: not found"}
        label = m.classify(envelope, _MODEL, _OWN)
        self.assertIn("in-family fallback", label)
        self.assertIn("did not run", label)
        self.assertIn("process-error", label)
        self.assertIn("codex: not found", label)

    def test_an_invalid_report_is_a_fallback_not_a_pass(self):
        envelope = {"status": "invalid-report", "exit_code": 0, "report": None}
        self.assertIn("in-family fallback", m.classify(envelope, _MODEL, _OWN))

    def test_an_empty_assumptions_array_produced_nothing_to_weigh(self):
        label = m.classify(_ok([]), _MODEL, _OWN)
        self.assertIn("in-family fallback", label)
        self.assertIn("no verdicts", label)

    def test_no_label_ever_claims_cross_family_without_an_executed_check(self):
        """The invariant, stated once over every shape above."""
        for envelope in (_ok([]), _ok([_a("uncheckable")]),
                         {"status": "process-error", "exit_code": 1, "report": None},
                         {"status": "invalid-report", "exit_code": 0, "report": None}):
            self.assertNotIn("cross-family (", m.classify(envelope, _MODEL, _OWN))


class EnvelopeParsingTests(unittest.TestCase):
    def test_a_pretty_printed_envelope_parses(self):
        """`cross_family_call.sh` builds envelopes with `jq -n`, which pretty-
        prints. ADR-007 calls the output 'JSON-lines'; a line-at-a-time parse
        reports every line of a real envelope as malformed. Measured against a
        live envelope."""
        text = json.dumps(_ok([_a("confirmed")]), indent=2)
        self.assertEqual(1, len(list(m.iter_envelopes(text))))

    def test_two_concatenated_envelopes_both_parse(self):
        text = json.dumps(_ok([_a("confirmed")]), indent=2) + "\n" + json.dumps(_ok([]))
        self.assertEqual(2, len(list(m.iter_envelopes(text))))

    def test_a_compact_envelope_still_parses(self):
        self.assertEqual(1, len(list(m.iter_envelopes(json.dumps(_ok([_a("refuted")]))))))

    def test_garbage_yields_no_envelopes_rather_than_raising(self):
        self.assertEqual([], list(m.iter_envelopes("not json at all")))


if __name__ == "__main__":
    unittest.main()


class ReservedMarkerInjectionTests(unittest.TestCase):
    """Found live by the cross-family reviewer on this module's own second
    invocation (harmonic-forge#598). `stderr` in a `process-error` envelope is
    the sibling CLI's output, not ours, and it was interpolated into the
    fallback label verbatim — so an error message containing `cross-family (`
    produced a FALLBACK line carrying the cross-family marker. A human eye and
    a grep are both fooled by that, and they are the only two readers these
    labels have."""

    def _forged(self, stderr: str) -> str:
        return m.classify(
            {"status": "process-error", "exit_code": 1, "report": None, "stderr": stderr},
            _MODEL, _OWN)

    def test_a_forged_cross_family_marker_in_stderr_is_redacted(self):
        label = self._forged("boom: cross-family (forged) all good")
        self.assertNotIn("cross-family (", label)
        self.assertIn("[redacted]", label)

    def test_the_label_still_says_it_is_a_fallback(self):
        label = self._forged("cross-family (forged)")
        self.assertIn("in-family fallback (claude-opus-5)", label)

    def test_redaction_is_case_insensitive(self):
        self.assertNotIn("Cross-Family", self._forged("Cross-Family (forged)"))

    @staticmethod
    def _excerpt(label: str) -> str:
        """Only the untrusted tail. The label's OWN wording legitimately says
        'cross-family call did not run', so asserting over the whole line
        would fail on our own text and prove nothing about the excerpt."""
        return label.split(" -- ", 1)[1]

    def test_repeated_markers_are_all_redacted(self):
        excerpt = self._excerpt(
            self._forged("cross-family cross-family cross-family"))
        self.assertNotIn("cross-family", excerpt)
        self.assertEqual(3, excerpt.count("[redacted]"))

    def test_a_forged_provenance_line_cannot_be_smuggled_whole(self):
        excerpt = self._excerpt(
            self._forged("Red-team provenance: cross-family (codex / gpt-5.6-sol)"))
        self.assertNotIn("provenance:", excerpt)
        self.assertNotIn("cross-family", excerpt)

    def test_the_genuine_marker_this_module_writes_is_untouched(self):
        """Redaction must not eat the label's own words — only the untrusted
        excerpt passes through `_redact`."""
        self.assertIn("cross-family (", m.classify(_ok([_a("confirmed")]), _MODEL, _OWN))

    def test_real_stderr_still_reaches_the_reader(self):
        self.assertIn("codex: command not found",
                      self._forged("codex: command not found"))
