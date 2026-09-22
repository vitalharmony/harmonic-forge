#!/usr/bin/env python3
"""a private-repo incident — `rework` is a real `--kind`, and deliberately a bare one on
heading: no mandated heading was invented for it, unlike `sweep`/`ae`.

a private-repo incident gave it a LEAD_FIELDS entry (`Finding`, `Next`) — a Lane 1 rework
request is exactly the shape that needs "what's the problem, what happens
next" before any evidence, the same case sweep/ae already made. What stays
bare is the heading requirement: still no mandated `## Rework` string, only
the lead-field content.
"""
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import l1_post as L  # noqa: E402

SCRIPT = Path(__file__).resolve().parent / "l1_post.py"


class ReworkKindTests(unittest.TestCase):
    def test_rework_is_an_accepted_kind(self) -> None:
        """The defect this closes is that it was not."""
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--issue", "1", "--kind", "rework",
             "--sha", "HEAD", "--branch", "nope", "--file", "/nonexistent"],
            capture_output=True, text=True,
        )
        self.assertNotIn("invalid choice", result.stderr,
                         "`rework` must be a valid --kind")

    def test_an_unknown_kind_is_still_refused(self) -> None:
        """AC6's other direction: the choices list is still a closed set, so a
        typo is a hard argparse failure rather than a silently-stamped footer
        nothing downstream can read."""
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--issue", "1", "--kind", "reworkk",
             "--sha", "HEAD", "--branch", "nope", "--file", "/nonexistent"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid choice", result.stderr)

    def test_rework_requires_a_finding_and_a_next_step(self) -> None:
        """a private-repo incident: a Lane 1 rework request is exactly the "what's the
        problem, what happens next" shape sweep/ae already require — a bare
        `## L1 — rework, H1\n\nrebase and push\n` is no longer sufficient."""
        self.assertEqual(L.LEAD_FIELDS["rework"], ("Finding", "Next"))
        with self.assertRaises(SystemExit):
            L.validate_lead("rework", "## L1 — rework, H1\n\nrebase and push\n")
        L.validate_lead(
            "rework",
            "## L1 — rework, H1\n\n**Finding:** branch is behind main.\n"
            "**Next:** rebase and re-push.\n\ndetail\n",
        )

    def test_rework_still_carries_no_mandated_heading(self) -> None:
        """The heading half of the original decision holds — only the
        content requirement changed. `AE_HEADING`/`SWEEP_HEADING` have no
        `REWORK_HEADING` counterpart, and this issue does not add one."""
        self.assertFalse(hasattr(L, "REWORK_HEADING"))

if __name__ == "__main__":
    unittest.main()
