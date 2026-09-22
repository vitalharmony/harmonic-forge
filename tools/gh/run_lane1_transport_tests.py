#!/usr/bin/env python3
"""Run the platform-owned Lane 1 transport tests without touching live belt state."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1]
GH_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS_DIR))

from run_tests import redirected_belt_candidates_dir  # noqa: E402


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for pattern in ("test_l1_post*.py", "test_post_lane_discussion*.py"):
        suite.addTests(loader.discover(str(GH_DIR), pattern=pattern))
    with tempfile.TemporaryDirectory(prefix="lane1-transport-tests-") as tmp:
        with redirected_belt_candidates_dir(Path(tmp) / "belt-candidates"):
            result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
