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

    def test_claude_verify_label_uses_the_recorded_pinned_model(self):
        envelope = _ok([_a("confirmed")]) | {"family": "claude", "verify_model": "claude-opus-5-5"}
        label = m.classify(envelope, _MODEL, "gpt-6-sol")  # a Codex session, reviewed by Claude
        self.assertIn("claude / claude-opus-5-5", label)

    def test_codex_label_uses_the_recorded_model_not_the_flag(self):
        """harmonic-forge#848 AC7: the envelope's verify_model wins for every
        family; --model is only the fallback for pre-#848 envelopes."""
        envelope = _ok([_a("confirmed")]) | {"family": "codex", "verify_model": "gpt-6-sol"}
        label = m.classify(envelope, "gpt-5.6-sol", _OWN)
        self.assertIn("gpt-6-sol", label)
        self.assertNotIn("gpt-5.6-sol", label)

    def test_codex_label_falls_back_to_the_flag_without_verify_model(self):
        envelope = _ok([_a("confirmed")]) | {"family": "codex"}
        self.assertIn("gpt-fallback-x", m.classify(envelope, "gpt-fallback-x", _OWN))

    def test_model_flag_default_is_gpt_6_sol(self):
        import contextlib, io, json as _json, tempfile as _tf
        with _tf.TemporaryDirectory() as tmp:
            path = Path(tmp) / "envelope.jsonl"
            path.write_text(_json.dumps(_ok([_a("confirmed")]) | {"family": "codex"}) + "\n")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                m.main(["--envelope", str(path), "--own-model", _OWN])
        self.assertIn("gpt-6-sol", out.getvalue())

    def test_same_family_review_is_not_labelled_cross_family(self):
        """Preclose finding 1: a Claude session that passes `--caller codex`
        gets a Claude reviewer. The label checks --own-model, not the
        self-declared caller, and refuses to call that cross-family."""
        envelope = _ok([_a("confirmed")]) | {"family": "claude", "verify_model": "claude-opus-5-5",
                                             "caller_family": "codex", "target_family": "claude"}
        # Sticky-wicket PATCH item 3: a --caller that is not the session's own
        # family is an invocation error, raised rather than labeled.
        with self.assertRaises(m.CallerMismatch):
            m.classify(envelope, "gpt-6-sol", "claude-opus-5-5")

    def test_same_family_review_without_caller_field_is_refused(self):
        """The equality refusal still holds for an envelope with no caller_family."""
        envelope = _ok([_a("confirmed")]) | {"family": "claude", "verify_model": "claude-opus-5-5"}
        label = m.classify(envelope, "gpt-6-sol", "claude-opus-5-5")
        self.assertNotIn("cross-family (", label)
        self.assertIn("in-family fallback", label)

    def test_unrecognized_own_model_refuses_the_cross_family_label(self):
        """Preclose pass 2 survivor 2: undecidable identity refuses the label."""
        for own in ("sol", "5.5", "mystery"):
            with self.subTest(own=own):
                label = m.classify(_ok([_a("confirmed")]) | {"verify_model": "gpt-6-sol"},
                                   "gpt-6-sol", own)
                self.assertNotIn("cross-family (", label)
                self.assertIn("names no known model family", label)

    def test_caller_mismatch_exits_2_and_prints_no_label(self):
        """Preclose pass 2 survivor 3: nothing reaches stdout, so nothing is recorded."""
        import contextlib, io, json as _json, tempfile as _tf
        envelope = _ok([_a("confirmed")]) | {"family": "claude", "caller_family": "codex",
                                             "target_family": "claude"}
        with _tf.TemporaryDirectory() as tmp:
            path = Path(tmp) / "envelope.jsonl"
            path.write_text(_json.dumps(envelope) + "\n")
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = m.main(["--envelope", str(path), "--own-model", "claude-opus-5-5"])
        self.assertEqual(code, 2)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("--caller claude", err.getvalue())

    def test_own_model_is_required(self):
        """Preclose pass 2 survivor 1: no default family, ever."""
        import contextlib, io
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as done:
            m.main(["--envelope", "/dev/null", "--not-triggered"])
        self.assertEqual(done.exception.code, 2)

    def test_codex_session_reviewed_by_codex_is_not_cross_family(self):
        label = m.classify(_ok([_a("confirmed")]) | {"verify_model": "gpt-6-sol"},
                           "gpt-6-sol", "gpt-6-sol")
        self.assertIn("in-family fallback", label)

    def test_claude_envelope_without_verify_model_never_names_the_codex_flag(self):
        """Preclose finding 4: the --model fallback is Codex's default and is
        never put in another family's label."""
        envelope = _ok([_a("confirmed")]) | {"family": "claude"}
        label = m.classify(envelope, "gpt-6-sol", "gpt-6-sol")
        self.assertIn("claude / unknown", label)
        self.assertNotIn("gpt-6-sol)", label)

    def test_model_family(self):
        cases = {"gpt-6-sol": "codex", "codex-mini": "codex", "claude-opus-5-5": "claude",
                 "opus": "claude", "claude-fable-5-1": "claude", "gemini-3": "gemini",
                 "mystery": None, None: None}
        for model, family in cases.items():
            with self.subTest(model=model):
                self.assertEqual(m.model_family(model), family)

    def test_not_triggered_label_names_the_calling_model(self):
        """harmonic-forge#848 AC8: --own-model is the calling session's model."""
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            m.main(["--envelope", "/dev/null", "--not-triggered", "--own-model", "gpt-6-sol"])
        self.assertIn("gpt-6-sol", out.getvalue())

    def test_codex_caller_review_is_labelled_claude(self):
        envelope = _ok([_a("confirmed")]) | {"family": "claude", "verify_model": "claude-opus-5-5",
                                             "caller_family": "codex", "target_family": "claude"}
        self.assertIn("cross-family (claude / claude-opus-5-5)",
                      m.classify(envelope, "gpt-6-sol", "gpt-6-sol"))

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
