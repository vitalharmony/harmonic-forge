#!/usr/bin/env python3
"""One gate-result recognizer for `lane_state` and `gate_ci` (harmonic-forge#1108).

Lives in `tools/gh` rather than beside `lane_state.py` because
`tools/run_tests.py` (the verification gate) does not discover
`skills/sprint-plan/scripts`; a test the gate never runs proves nothing.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from unittest import mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
LANE_STATE = HERE.parents[1] / "skills" / "sprint-plan" / "scripts" / "lane_state.py"

sys.path.insert(0, str(HERE))
import gate_ci  # noqa: E402
from test_gate_ci import check, fake_runs  # noqa: E402  (its routed gh fake)

_spec = importlib.util.spec_from_file_location("lane_state_1108", LANE_STATE)
lane_state = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(lane_state)

SHA = "abc1234def5678abc1234def5678abc1234def56"


def keys(body: str) -> set[str]:
    return {t.key for t in lane_state._scan_comment({"id": "1", "body": body})}


def scored(body: str) -> bool:
    return bool(keys(body) & {"gate.pass", "gate.fail"})


class TokenGateTests(unittest.TestCase):
    def test_a_l3p_token_runs_the_ci_check_and_refuses_on_red(self):
        """(a) AC3: a `## L3P` post makes `check_gate_result` run CI."""
        body = f"## L3P — H1\n\nHead-SHA: {SHA}\n"
        ok, message = gate_ci.check_gate_result(
            "o/r", body, run=fake_runs(check("verify", "failure")))
        self.assertFalse(ok, message)
        self.assertIn("RED", message)
        self.assertIn("gate.pass", keys(body))

    def test_a2_a_bare_l3f_token_posts_as_fail(self):
        """(a2) A FAIL must stay publishable."""
        body = "## L3F — H1\n\nTC3 failed.\n"
        self.assertEqual(gate_ci.verdict_of(body), "FAIL")
        ok, message = gate_ci.check_gate_result(
            "o/r", body, run=fake_runs(check("verify", "failure")))
        self.assertTrue(ok, message)
        self.assertIn("gate.fail", keys(body))

    def test_token_and_heading_disagreeing_is_conflict(self):
        body = "## L3P — H1\n\n**Verdict:** FAIL\n"
        self.assertEqual(gate_ci.verdict_of(body), "CONFLICT")

    def test_a4_lane_state_loads_from_a_clean_sys_path(self):
        """(a4) `lane_state` finds `tools/gh` itself, and reads gate_ci's heading."""
        code = (
            "import importlib.util,sys\n"
            f"s=importlib.util.spec_from_file_location('ls',{str(LANE_STATE)!r})\n"
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\n"
            "assert m._GATE_HEADING.search('   ##### Lane 3 Gate Results — PASS')\n"
        )
        result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True,
                                text=True, cwd="/")
        self.assertEqual(result.returncode, 0, result.stderr)


class LaneStateRecognitionTests(unittest.TestCase):
    def test_b_an_indented_h5_gate_heading_is_scored(self):
        """(b) AC2: ATX 0-3 spaces, 1-6 `#`, as `gate_ci.GATE_HEADING`."""
        self.assertIn("gate.pass", keys("   ##### Lane 3 Gate Results — PASS\n"))

    def test_c_a_tilde_fenced_gate_heading_is_not_scored(self):
        """(c) AC1: stripped with `markdown_strip.visible_text`."""
        self.assertFalse(scored("quoted:\n\n~~~\n## Lane 3 Gate Results — PASS\n~~~\n"))

    def test_c3_a_tilde_fenced_spec_heading_is_not_scored(self):
        """AC1 on the markers lane_state still reads itself: a quoted spec is
        stripped by `markdown_strip.visible_text`, not a backtick-only regex."""
        self.assertNotIn("spec.posted",
                         keys("quoted:\n\n~~~\n## Lane 3 Test Spec — H1\n~~~\n"))

    def test_c2_a_blockquoted_l3p_is_not_scored(self):
        """(c2) Not scored, and the quote is removed by the shared stripper
        `gate_ci` reads, not by an accident of `lane_state`'s own regex."""
        body = "> ## L3P — H1\n"
        with mock.patch.object(lane_state.markdown_strip, "visible_text",
                               wraps=lane_state.markdown_strip.visible_text) as spy:
            self.assertFalse(scored(body))
        spy.assert_any_call(body)


#: (d) AC4's shared corpus.
CORPUS = [
    "## Lane 3 Gate Results — PASS\n",
    "## Lane 3 Gate Results — FAIL\n",
    "```\n## Lane 3 Gate Results — PASS\n```\n",
    "````\n```\n## Lane 3 Gate Results — PASS\n```\n````\n",
    "~~~\n## Lane 3 Gate Results — PASS\n~~~\n",
    "> ## Lane 3 Gate Results — PASS\n",
    "> ## L3P — H1\n",
    "text\n\n    ## Lane 3 Gate Results — PASS\n",
    "## L3P — H1\n",
    "## L3F — H1\n",
    "   ## L3P — H1\n",
    "```\n## L3P — H1\n```\n",
    "Some prose first.\n\n## Lane 3 Gate Results — PASS\n",
    "Some prose first.\n\n## L3P — H1\n",
] + [f"{' ' * (n % 4)}{'#' * n} Lane 3 Gate Results — {v}\n"
     for n in range(1, 7) for v in ("PASS", "FAIL")]


class SharedCorpusPropertyTests(unittest.TestCase):
    def test_d_every_scored_gate_is_a_possible_gate_report(self):
        """(d) AC4: whatever `lane_state` scores, `gate_ci` checks."""
        for body in CORPUS:
            with self.subTest(body=body):
                if scored(body):
                    self.assertTrue(gate_ci.may_be_a_gate_report(body))

    def test_d_the_corpus_is_not_vacuous(self):
        self.assertGreaterEqual(sum(scored(b) for b in CORPUS), 16)
        self.assertIn("gate.pass", keys("## L3P — H1\n"))


#: The sticky-wicket survivor bodies (harmonic-forge#1108 pass 1).
AGREEMENT_CORPUS = CORPUS + [
    "##\nL3P — H1\n",
    "## Lane 3 Gate Results — BLOCKED (PASS once CI is green)\n",
    "## Lane 3 Gate Results — BLOCKED\n\nCI not green.\n\n"
    "## Lane 3 Gate Results — PASS\n",
]


class OneRecognizerTests(unittest.TestCase):
    def test_e_lane_state_scores_exactly_what_gate_ci_reads(self):
        """lane_state has no lane-3 verdict of its own: gate.pass iff
        verdict_of is PASS, gate.fail iff FAIL, on every corpus body."""
        for body in AGREEMENT_CORPUS:
            with self.subTest(body=body):
                got, verdict = keys(body), gate_ci.verdict_of(body)
                self.assertEqual("gate.pass" in got, verdict == "PASS")
                self.assertEqual("gate.fail" in got, verdict == "FAIL")

    def test_f_lane_state_carries_no_l3_verdict_regex(self):
        """Guard: no regex literal in lane_state.py names PASS or the gate
        heading — the only recognizer is gate_ci's."""
        src = LANE_STATE.read_text()
        code = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
        regexy = [ln for ln in code
                  if "re.compile" in ln or ' = r"' in ln or " = r'" in ln
                  or ln.lstrip().startswith(('r"', "r'"))]
        bad = [ln for ln in regexy if "PASS" in ln or "Gate Results" in ln]
        self.assertEqual(bad, [])

    def test_g_eras_extractor_version_bumped_for_new_scoring(self):
        """eras.py's docstring: parsing changes bump EXTRACTOR_VERSION, so
        rows scored by the old lane-3 recognizer are not mixed with new."""
        src = (HERE / ".." / "telemetry" / "eras.py").resolve().read_text()
        self.assertIn('EXTRACTOR_VERSION = "threads-5"', src)


if __name__ == "__main__":
    unittest.main()
