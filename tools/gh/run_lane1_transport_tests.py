#!/usr/bin/env python3
"""Run the platform-owned Lane 1 transport tests without touching live belt state."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1]
GH_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS_DIR))

from run_tests import hermetic_identity_probe, redirected_belt_candidates_dir  # noqa: E402


def main() -> int:
    # harmonic-forge#865: inherited by any writer a test spawns as a subprocess.
    os.environ["HARMONIC_FORGE_TESTING"] = "1"
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for pattern in ("test_l1_post*.py", "test_post_lane_discussion*.py"):
        suite.addTests(loader.discover(str(GH_DIR), pattern=pattern))
    with tempfile.TemporaryDirectory(prefix="lane1-transport-tests-") as tmp:
        # harmonic-forge#907: and every emitted event to a throwaway store.
        # Forced, as run_tests.py does (harmonic-forge#892): this runner was the
        # one that leaked l1_post fixture events into the operator's store.
        os.environ["HARMONIC_FORGE_TELEMETRY_STORE"] = str(Path(tmp) / "telemetry-store")
        with redirected_belt_candidates_dir(Path(tmp) / "belt-candidates"), \
                hermetic_identity_probe():
            result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
