"""harmonic-forge#907 preclose passes 1 and 2: each repo's scheduled window
starts at that repo's last successful run, so a long gap is re-covered and one
failing repo never stalls the others' progress."""
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
REPOS = ["o/good", "o/flaky", "p/other"]


class PerRepoWatermarks(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def _run(self, now=NOW, failing=()):
        windows = {}

        def runner(argv):
            repo = argv[argv.index("--repo") + 1]
            windows[repo] = argv[argv.index("--since") + 1]
            return 1 if repo in failing else 0
        with redirect_stdout(io.StringIO()) as out:
            rc = job.main(now=now, runner=runner, repos=REPOS)
        return rc, windows, out.getvalue()

    def _mark(self, repo):
        path = job.watermark_path(repo)
        return path.read_text().strip() if path.exists() else None

    def test_no_watermarks_use_the_default_window_per_repo(self):
        rc, windows, _ = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(set(windows.values()), {"2026-10-17T05:05:00Z"})
        self.assertEqual(set(windows), set(REPOS), "each repo runs on its own")

    def test_one_failing_repo_never_blocks_the_others(self):
        rc, _, out = self._run(failing={"o/flaky"})
        self.assertEqual(rc, 1)
        self.assertEqual(self._mark("o/good"), "2026-10-20T05:05:00Z")
        self.assertEqual(self._mark("p/other"), "2026-10-20T05:05:00Z")
        self.assertIsNone(self._mark("o/flaky"))
        self.assertIn("1 of 3 repos failed: o/flaky", out)

    def test_the_failing_repos_next_window_widens_and_only_its(self):
        first = NOW
        self._run(now=first)                                   # all succeed once
        second = first + timedelta(days=5)
        self._run(now=second, failing={"o/flaky"})             # flaky fails
        third = second + timedelta(days=5)
        _, windows, _ = self._run(now=third)                   # all succeed
        self.assertEqual(windows["o/flaky"], "2026-10-19T05:05:00Z")   # its old mark minus overlap
        self.assertEqual(windows["o/good"], "2026-10-24T05:05:00Z")    # its recent mark minus overlap

    def test_a_watermark_beyond_the_lookback_is_clamped_and_said_so(self):
        path = job.watermark_path("o/good")
        path.parent.mkdir(parents=True)
        path.write_text("2026-08-01T00:00:00Z\n")
        _, windows, out = self._run()
        self.assertEqual(windows["o/good"], "2026-09-20T05:05:00Z")
        self.assertIn("CLAMPED", out)

    def test_an_unreadable_watermark_falls_back_to_the_default(self):
        path = job.watermark_path("o/good")
        path.parent.mkdir(parents=True)
        path.write_text("not a date\n")
        _, windows, _ = self._run()
        self.assertEqual(windows["o/good"], "2026-10-17T05:05:00Z")


class ManifestFailsLoud(unittest.TestCase):
    def test_an_unreadable_manifest_runs_nothing_and_exits_nonzero(self):
        ran = []

        def boom():
            raise RuntimeError("no manifest")
        with mock.patch.object(job, "manifest_repos", side_effect=lambda: boom()), \
                redirect_stdout(io.StringIO()):
            rc = job.main(now=NOW, runner=lambda argv: ran.append(argv) or 0)
        self.assertEqual(rc, 2)
        self.assertEqual(ran, [])

    def test_an_empty_manifest_exits_nonzero(self):
        with redirect_stdout(io.StringIO()):
            rc = job.main(now=NOW, runner=lambda argv: 0, repos=[])
        self.assertEqual(rc, 2)

    def test_the_real_manifest_names_repos(self):
        self.assertTrue(job.manifest_repos())


if __name__ == "__main__":
    unittest.main()
