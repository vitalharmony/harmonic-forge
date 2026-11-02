#!/usr/bin/env python3
"""harmonic-forge#1257: a test process never writes the operator's real
task-list marker store, whatever the session id; a redirected store still
records."""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tasklist_owed as owed  # noqa: E402


class RealStoreGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "s-guard"})
        env.start()
        self.addCleanup(env.stop)

    def test_the_real_store_is_never_written_under_test(self):
        # Both globals point at one temp dir standing in for the real store, so
        # a regression writes there and never to the operator's ~/.cache.
        real = self.tmp / "real"
        with mock.patch.object(owed, "REAL_OWED_DIR", real), \
             mock.patch.object(owed, "OWED_DIR", real):
            self.assertFalse(owed.record("F5", "handoff"))
        self.assertFalse(real.exists() and any(real.rglob("*.json")))

    def test_a_redirected_store_still_records(self):
        real, redirected = self.tmp / "real", self.tmp / "redirected"
        with mock.patch.object(owed, "REAL_OWED_DIR", real), \
             mock.patch.object(owed, "OWED_DIR", redirected):
            self.assertTrue(owed.record("F5", "handoff"))
        self.assertTrue((redirected / "s-guard" / "F5.json").exists())
        self.assertFalse(real.exists())


if __name__ == "__main__":
    unittest.main()
