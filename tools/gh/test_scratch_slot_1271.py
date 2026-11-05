"""harmonic-forge#1271: a scratch run takes capacity before disk, and a nested one takes none."""
from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import _scratch  # noqa: E402


class ScratchSlot(unittest.TestCase):
    def setUp(self):
        cache = Path.home() / ".cache"  # the scratch root refuses anything under /tmp
        cache.mkdir(exist_ok=True)
        tmp = tempfile.TemporaryDirectory(dir=cache, prefix="scratch-slot-test-")
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.dict(os.environ, {"HRSE_SCRATCH_ROOT": tmp.name, "HRSE_SCRATCH_MIN_FREE_GB": "0"})
        patch.start()
        self.addCleanup(patch.stop)

    def _traced(self, order):
        @contextlib.contextmanager
        def slot(prefix, label):
            order.append(("slot", prefix))
            yield

        @contextlib.contextmanager
        def lock(base, label):
            order.append(("lock", label))
            yield

        return mock.patch.object(_scratch, "_heavy_slot", slot), mock.patch.object(_scratch, "_lock", lock)

    def test_capacity_is_taken_before_the_disk_lock(self):
        order: list = []
        a, b = self._traced(order)
        with a, b, _scratch.scratch_dir("t", prefix="kill-check-"):
            pass
        self.assertEqual([k for k, _ in order], ["slot", "lock"])

    def test_a_nested_scratch_takes_neither(self):
        order: list = []
        a, b = self._traced(order)
        with a, b, _scratch.scratch_dir("t", lock=False):
            pass
        self.assertEqual(order, [])

    def test_a_missing_tool_runs_unmanaged(self):
        with mock.patch.dict(sys.modules, {"heavy_slot": None}):
            with _scratch._heavy_slot("l1-post-", "t"):
                pass

    def test_the_kind_follows_the_prefix(self):
        with mock.patch.dict(sys.modules, {"heavy_slot": mock.Mock()}):
            _scratch._heavy_slot("l1-post-", "t")
            sys.modules["heavy_slot"].slot.assert_called_with("check", "t")
            _scratch._heavy_slot("kill-check-", "t")
            sys.modules["heavy_slot"].slot.assert_called_with("kill", "t")


if __name__ == "__main__":
    unittest.main()
