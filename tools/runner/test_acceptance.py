"""harmonic-forge#1112 AC11: one acceptance-heading matcher, shared by the
preclose driver and `te_run.ac_titles`."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import acceptance  # noqa: E402
import preclose_driver  # noqa: E402
import te_run  # noqa: E402

#: F1073's shape: `## Acceptance` and checkbox items.
F1073_BODY = ("## Problem\n\nSomething.\n\n## Acceptance\n\n"
              "- [ ] The emitter writes one line per event.\n"
              "- [ ] A missing field is refused.\n\n## Notes\n\nnot a criterion\n")


class Shared(unittest.TestCase):
    def test_f1073_shaped_body_is_read_by_driver_and_ac_titles(self):
        with mock.patch.object(preclose_driver, "issue_body", return_value=F1073_BODY):
            criteria = preclose_driver.acceptance_criteria("vitalharmony/harmonic-forge", 1073, {})
        self.assertIn("one line per event", criteria)
        self.assertNotIn("not a criterion", criteria)
        self.assertEqual(te_run.ac_titles(F1073_BODY),
                         ["The emitter writes one line per event.", "A missing field is refused."])

    def test_heading_variants(self):
        for heading in ("## Acceptance criteria", "## acceptance", "### Acceptance Criteria (amended)",
                        "## ACCEPTANCE criteria:"):
            body = f"{heading}\n\n1. One.\n\n## Next\n\nx\n"
            self.assertEqual(acceptance.items(acceptance.section(body)), ["One."], heading)

    def test_section_ends_at_same_or_higher_level_only(self):
        body = "## Acceptance\n\n1. One.\n\n### Detail\n\n2. Two.\n\n## Next\n\n3. Three.\n"
        self.assertEqual(acceptance.items(acceptance.section(body)), ["One.", "Two."])

    def test_no_section(self):
        self.assertIsNone(acceptance.section("## Acceptable risk\n\n1. x\n"))
        with mock.patch.object(preclose_driver, "issue_body", return_value="## Problem\n\nx\n"):
            with self.assertRaises(preclose_driver.DriverError):
                preclose_driver.acceptance_criteria("vitalharmony/harmonic-forge", 1, {})


if __name__ == "__main__":
    unittest.main()
