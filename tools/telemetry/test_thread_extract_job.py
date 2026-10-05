"""harmonic-forge#907 preclose pass 1: the scheduled extraction's window
starts at the last successful run, so a gap longer than the default window
is re-covered rather than silently lost."""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import thread_extract_job as job  # noqa: E402

NOW = datetime(2026, 10, 20, 5, 5, tzinfo=timezone.utc)


class WatermarkWindow(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.mark = job.watermark_path()

    def _run(self, code=0):
        calls = []
        with redirect_stdout(io.StringIO()):
            rc = job.main(now=NOW, runner=lambda argv: calls.append(argv) or code)
        return rc, calls[0][calls[0].index("--since") + 1]

    def test_no_watermark_uses_the_default_window(self):
        _, since = self._run()
        self.assertEqual(since, "2026-10-17T05:05:00Z")

    def test_a_gap_longer_than_the_window_is_re_covered(self):
        self.mark.parent.mkdir(parents=True)
        self.mark.write_text("2026-10-10T05:05:00Z\n")   # ten days ago
        _, since = self._run()
        self.assertEqual(since, "2026-10-09T05:05:00Z")   # watermark minus the overlap

    def test_a_failed_run_keeps_the_watermark(self):
        self.mark.parent.mkdir(parents=True)
        self.mark.write_text("2026-10-10T05:05:00Z\n")
        rc, _ = self._run(code=1)
        self.assertEqual(rc, 1)
        self.assertEqual(self.mark.read_text().strip(), "2026-10-10T05:05:00Z")

    def test_a_successful_run_advances_the_watermark_to_its_start(self):
        rc, _ = self._run(code=0)
        self.assertEqual(rc, 0)
        self.assertEqual(self.mark.read_text().strip(), "2026-10-20T05:05:00Z")

    def test_an_unreadable_watermark_falls_back_to_the_default(self):
        self.mark.parent.mkdir(parents=True)
        self.mark.write_text("not a date\n")
        _, since = self._run()
        self.assertEqual(since, "2026-10-17T05:05:00Z")


if __name__ == "__main__":
    unittest.main()
