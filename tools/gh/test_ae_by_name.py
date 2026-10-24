#!/usr/bin/env python3
"""harmonic-forge#1179 (reforged): one `**AE by name:**` grammar, one verdict at every entry point."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ae_by_name as G  # noqa: E402
import l1_post as L  # noqa: E402
import post_lane_discussion as P  # noqa: E402

#: Every spec line passes 1 and 2 found, plus the plain good forms. Each maps
#: to the flagged set, or None for "refused".
SPEC_CASES = {
    "": set(),
    "**AE by name:** none\n": set(),
    "**AE by name:** TC1: the lookup; TC2: the install\n": {"1", "2"},
    "**AE by name:** TC01: the lookup\n": {"1"},
    "- **AE by name:** TC1: the lookup\n": None,
    "**AE by name**: TC1: the lookup\n": None,
    "**Needing AE by name:** TC1 runs the lookup\n": None,
    "**AE by name:** TC1 and TC2\n": None,
    "**AE by name:** TC1\n": None,
    "**AE by name:** TC1 lookup\n": None,
    "**AE by name:** TC1:; TC2: install\n": None,
    "**AE by name:** TC1: x;\n": None,
    "**AE by name:** none\n**AE by name:** TC1: x\n": None,
    "**AE-by-name:** TC1: the lookup\n": None,
    "**AE_by_name:** TC1: the lookup\n": None,
    "**AE **by** name:** TC1: the lookup\n": None,
    "**AEbyname:** TC1: the lookup\n": None,
    "```\n**AE by name:** nonsense\n```\n**AE by name:** none\n": set(),
}


def _spec_post_verdict(body: str):
    try:
        P.require_ae_by_name(body)
    except SystemExit:
        return None
    return "ok"


def _ae_time_verdict(body: str):
    try:
        return L.spec_ae_by_name(body)
    except SystemExit:
        return None


class OneGrammarEveryEntryPoint(unittest.TestCase):
    def test_spec_post_and_ae_time_agree_on_every_line(self) -> None:
        for body, expected in SPEC_CASES.items():
            with self.subTest(body=body):
                self.assertEqual(_ae_time_verdict(body), expected)
                self.assertEqual(_spec_post_verdict(body), None if expected is None else "ok")

    def test_padded_and_unpadded_ids_are_one_case(self) -> None:
        spec = "**AE by name:** TC01: the lookup\n"
        L.validate_ae_by_name(spec, "**AE by name:** TC1\n", "## Gate-readiness sweep — F1\n")
        L.validate_ae_by_name("**AE by name:** TC1: x\n", "**AE by name:** TC001\n", "## sweep\n")

    def test_an_ae_line_that_does_not_parse_refuses(self) -> None:
        for ae in ("**AE by name:** TC1 and TC2\n", "- **AE by name:** TC1\n",
                   "**AE by name:** TC1\n**AE by name:** TC2\n"):
            with self.subTest(ae=ae), self.assertRaises(SystemExit):
                L.validate_ae_by_name("**AE by name:** TC1: x\n", ae, None)

    def test_one_canonicalizer_for_every_caller(self) -> None:
        """Reforge pass 1, survivor 2: the live-case checks agree with the by-name check."""
        for case in ("TC01", "TC1", "tc 1", "P1", "1", "TC-001"):
            with self.subTest(case=case):
                self.assertEqual(L._canonical_case_id(case), "1")
                self.assertEqual(L._canonical_case_id(case), G.canonical_id(case))

    def test_carried_ids_are_canonical(self) -> None:
        self.assertEqual(G.carried("2. TC02 — carried forward, not re-run: x\n"), {"2"})


class SpecPostCallSite(unittest.TestCase):
    """Pass 2 survivor 4: the refusal is wired into `--kind spec`, not only the helper."""

    SPEC = ("## Lane 3 Test Spec — H1\n\n**Status:** derived.\n**Cases:** 1.\n"
            "Write tier: R\n**Next:** Lane 1 reviews.\n\n### Test cases\n| TC | x |\n|---|---|\n| TC1 | y |\n")

    def _post(self, body: str) -> list:
        posted = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spec.md"
            path.write_text(body)
            argv = ["post_lane_discussion.py", "--issue", "1", "--file", str(path), "--kind", "spec"]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.dict("os.environ", {"LANE": "3"}), \
                 mock.patch.object(P, "comment_body",
                                   side_effect=lambda *a: posted.append(a) or ("u", 1)):
                P.main()
        return posted

    def test_the_clean_spec_posts(self) -> None:
        """The control: without it, the refusal below could come from anything."""
        self.assertEqual(len(self._post(self.SPEC)), 1)

    def test_a_malformed_line_never_reaches_the_transport(self) -> None:
        body = self.SPEC.replace("**Next:**", "- **AE by name:** TC1: the lookup\n**Next:**")
        with self.assertRaises(SystemExit):
            self._post(body)


if __name__ == "__main__":
    unittest.main()
