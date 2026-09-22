#!/usr/bin/env python3
"""Anonymized corpus-shaped regression cases for Lane 1 lead validation."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import l1_post as L  # noqa: E402

UNFOLDED_SPEC = """## Lane 3 Test Spec — X1

This deliberately long preamble has no organizational heading or details fold.
""" + ("substantive evidence without a fold " * 180)

READY_WITH_FOLD = """# ready-for-l3 — X1

**Target:** `branch` @ `abcdef0`.
**Base:** current.

## Review

Detailed evidence lives here and must not count toward the lead cap.
"""

GATE_WITHOUT_VERDICT = """## Lane 3 Gate Results — X1 — FAIL

**Finding:** the named case failed reproducibly.
**Next:** return to implementation.

<details><summary>Evidence</summary>

Detailed output.
</details>
"""


class AnonymizedCorpusDemonstration(unittest.TestCase):
    def test_unfolded_spec_is_over_the_lead_cap(self) -> None:
        self.assertEqual(L.lead_region(UNFOLDED_SPEC), UNFOLDED_SPEC)
        with self.assertRaises(SystemExit):
            L.validate_lead("spec", UNFOLDED_SPEC)

    def test_ready_for_l3_stops_at_the_first_section(self) -> None:
        region = L.lead_region(READY_WITH_FOLD)
        self.assertNotIn("Detailed evidence", region)
        self.assertLess(len(region.encode()), L.LEAD_CAP_BYTES)

    def test_ready_for_l3_requires_its_missing_fields(self) -> None:
        with self.assertRaises(SystemExit):
            L.validate_lead("ready-for-l3", READY_WITH_FOLD)
        completed = READY_WITH_FOLD.replace(
            "**Base:** current.",
            "**Base:** current.\n**Verified:** checks green.\n**Next:** Lane 3 executes.",
        )
        L.validate_lead("ready-for-l3", completed)

    def test_gate_result_requires_verdict_even_with_other_fields(self) -> None:
        with self.assertRaises(SystemExit):
            L.validate_lead("gate-result", GATE_WITHOUT_VERDICT)


if __name__ == "__main__":
    unittest.main()
