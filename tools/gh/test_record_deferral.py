#!/usr/bin/env python3
"""Unit tests for record_deferral.py (harmonic-forge#1564 AC11): explicit
keys, at most 25, and no `gh` or network call."""
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deferral_store  # noqa: E402  (harmonic-forge#1564)
import record_deferral  # noqa: E402

REPO = "vitalharmony/hrse"


class RecordDeferralCliTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        patcher = mock.patch.object(deferral_store, "DEFAULT_CANDIDATES_DIR", self.base)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = record_deferral.main(argv)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_records_the_named_keys_with_no_gh_call(self):
        def refuse(*_a, **_k):
            raise AssertionError("record_deferral.py must make no subprocess call")
        with mock.patch("subprocess.run", side_effect=refuse), \
             mock.patch("subprocess.Popen", side_effect=refuse), \
             mock.patch("urllib.request.urlopen", side_effect=refuse):
            code, out, _ = self._main(["--repo", REPO, "--issue", "2757", "--issue", "2778"])
        self.assertEqual(code, 0)
        self.assertEqual(deferral_store.read_deferrals([REPO]), {(REPO, 2757), (REPO, 2778)})
        self.assertEqual(deferral_store.deferral_entry(REPO, 2757)["source"], "backfill")
        self.assertIn(f"recorded deferral {REPO}#2757", out)

    def test_refuses_more_than_25_keys(self):
        argv = ["--repo", REPO]
        for n in range(1, 27):
            argv += ["--issue", str(n)]
        code, _, err = self._main(argv)
        self.assertEqual(code, 2)
        self.assertIn("at most 25", err)
        self.assertEqual(deferral_store.read_deferrals([REPO]), set())

    def test_25_keys_are_accepted(self):
        argv = ["--repo", REPO]
        for n in range(1, 26):
            argv += ["--issue", str(n)]
        code, _, _ = self._main(argv)
        self.assertEqual(code, 0)
        self.assertEqual(len(deferral_store.read_deferrals([REPO])), 25)

    def test_refuses_to_run_with_no_issue(self):
        code, _, _ = self._main(["--repo", REPO])
        self.assertEqual(code, 2)
        self.assertEqual(deferral_store.read_deferrals([REPO]), set())

    def test_refuses_to_run_with_no_repo(self):
        code, _, _ = self._main(["--issue", "1"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
