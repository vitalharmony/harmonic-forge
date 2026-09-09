#!/usr/bin/env python3
"""Tests for the belt wake-up hook (harmonic-forge#518 AC7)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from belt_wakeup import build_wakeup, handle  # noqa: E402


class TestWakeupStatesTheRole(unittest.TestCase):
    def test_each_lane_gets_its_own_role_line(self):
        for lane in ("1", "2", "3"):
            text = build_wakeup(lane, "env")
            self.assertIsNotNone(text)
            self.assertIn(f"LANE={lane}", text)

    def test_lane_2_is_not_told_it_may_merge_or_close(self):
        """The defect the whole issue exists for, in the one place a session
        reads before anything else."""
        text = build_wakeup("2", "env").lower()
        self.assertIn("never push, merge, close", text)
        self.assertNotIn("ae-and-sweep;", text)

    def test_lane_1_carries_its_merge_authority(self):
        self.assertIn("merge and close", build_wakeup("1", "env"))


class TestBeltDoesNotAutoArm(unittest.TestCase):
    """AC7's second half, and the operator decision behind it."""

    def test_every_role_line_says_not_armed(self):
        for lane in ("1", "2", "3"):
            self.assertIn("NOT armed", build_wakeup(lane, "env"))

    def test_the_hook_arms_nothing(self):
        """A source-level assertion, because this is the kind of thing that
        gets added later 'for convenience' and reverses an operator decision
        silently."""
        src = (Path(__file__).parent / "belt_wakeup.py").read_text(encoding="utf-8")
        body = src.split('"""', 2)[-1]  # skip the module docstring
        for forbidden in ("Monitor(", "CronCreate", "subprocess.run", "os.system"):
            self.assertNotIn(forbidden, body, f"wake-up must not arm anything: {forbidden}")


class TestNoLaneMeansNoProtocols(unittest.TestCase):
    def test_unset_lane_produces_nothing(self):
        self.assertIsNone(build_wakeup("", "none"))
        self.assertIsNone(build_wakeup("unknown", "none"))

    def test_handle_is_silent_rather_than_erroring(self):
        """A no-lane session is normal, not a fault."""
        import os
        saved = os.environ.pop("LANE", None)
        try:
            self.assertEqual(handle({"cwd": "/tmp"}), {})
        finally:
            if saved is not None:
                os.environ["LANE"] = saved


class TestCompactionSeamCarriesIt(unittest.TestCase):
    """AC7: fires at SessionStart *and again* after compaction. A compacted
    session never sees `belt_wakeup`, because a different hook owns that
    matcher."""

    def test_build_context_mentions_the_skill(self):
        from compaction_marker import build_context
        text = build_context("2026-09-09T00:00:00Z", "2",
                             "/home/mmangus/Harmonic_Projects/HRSE2-lane2")
        self.assertIn("/belt-and-suspenders", text)
        self.assertIn("not armed", text.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
