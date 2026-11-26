"""harmonic-forge#1578 AC1: `handoff_owed.discharge` reports a failed store
write as -1 (the obligation may still stand), and a missing store as 0."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import handoff_owed  # noqa: E402

REPO = "o/r"


@unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory modes")
class DischargeReportsAFailedWrite(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name) / "handoff_owed"
        patcher = mock.patch.object(handoff_owed, "OWED_DIR", self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _lock(self, mode: int) -> None:
        self.store.chmod(mode)
        self.addCleanup(self.store.chmod, 0o700)

    def test_a_failed_write_returns_minus_one_and_the_record_stands(self):
        self.assertTrue(handoff_owed.record(REPO, 2, "u", key="s1"))
        self._lock(0o500)
        self.assertEqual(handoff_owed.discharge(REPO, 2), -1)
        self.assertEqual([e["issue"] for e in handoff_owed.outstanding("s1")], [2])

    def test_an_unlistable_store_returns_minus_one(self):
        self.assertTrue(handoff_owed.record(REPO, 2, "u", key="s1"))
        self._lock(0o000)
        self.assertEqual(handoff_owed.discharge(REPO, 2), -1)

    def test_an_unreadable_obligation_file_returns_minus_one(self):
        """Preclose round 1 survivor 7: never read as an empty record list."""
        self.assertTrue(handoff_owed.record(REPO, 2, "u", key="s1"))
        locked = self.store / "s1.json"
        locked.chmod(0o000)
        self.addCleanup(locked.chmod, 0o600)
        self.assertEqual(handoff_owed.discharge(REPO, 2), -1)

    def test_a_missing_store_is_zero(self):
        self.assertEqual(handoff_owed.discharge(REPO, 2), 0)

    def test_a_successful_write_returns_the_count(self):
        handoff_owed.record(REPO, 2, "u", key="s1")
        handoff_owed.record(REPO, 2, "u", key="s2")
        self.assertEqual(handoff_owed.discharge(REPO, 2), 2)
        self.assertEqual(handoff_owed.outstanding("s1"), [])


if __name__ == "__main__":
    unittest.main()
