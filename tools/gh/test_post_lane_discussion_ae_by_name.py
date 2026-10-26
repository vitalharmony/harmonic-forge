#!/usr/bin/env python3
"""harmonic-forge#1179 AC1: a spec's `**AE by name:**` line is structured."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import post_lane_discussion as m  # noqa: E402


class SpecAeByName(unittest.TestCase):
    def ok(self, body: str) -> None:
        m.require_ae_by_name(body)

    def refused(self, body: str) -> None:
        with self.assertRaises(SystemExit):
            m.require_ae_by_name(body)

    def test_absent_none_and_entries_are_accepted(self) -> None:
        self.ok("## Lane 3 Test Spec — H1\n")
        self.ok("**AE by name:** none\n")
        self.ok("**AE by name:** TC1: the advisory lookup; TC2: the scratch install\n")

    def test_a_malformed_or_doubled_line_is_refused(self) -> None:
        self.refused("**AE by name:** TC1 and TC2\n")
        self.refused("**AE by name:** TC1:\n")
        self.refused("**AE by name:** none\n**AE by name:** TC1: x\n")

    def test_a_near_miss_line_is_refused(self) -> None:
        """F1179 preclose pass 1, survivor 1: never read as absent."""
        self.refused("- **AE by name:** TC1: the advisory lookup\n")
        self.refused("**AE by name**: TC1: the advisory lookup\n")
        self.refused("**Needing AE by name:** TC1 runs the advisory lookup\n")

    def test_a_fenced_example_is_not_the_line(self) -> None:
        self.ok("```\n**AE by name:** nonsense\n```\n**AE by name:** none\n")


if __name__ == "__main__":
    unittest.main()
