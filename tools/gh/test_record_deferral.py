#!/usr/bin/env python3
"""Unit tests for record_deferral.py (harmonic-forge#1564 AC11, amended):
explicit keys, at most 25, a required `--operator-login`, an onboarded repo
under its canonical key, and no `gh` or network call."""
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
LOGIN = ["--operator-login", "vitalharmony"]


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
            code, out, _ = self._main(["--repo", REPO, "--issue", "2757", "--issue", "2778", *LOGIN])
        self.assertEqual(code, 0)
        self.assertEqual(deferral_store.read_deferrals([REPO]), {(REPO, 2757), (REPO, 2778)})
        self.assertEqual(deferral_store.deferral_entry(REPO, 2757)["source"], "backfill")
        self.assertEqual(deferral_store.deferral_entry(REPO, 2757)["operator_login"], "vitalharmony")
        self.assertIn(f"recorded deferral {REPO}#2757", out)

    def test_refuses_more_than_25_keys(self):
        argv = ["--repo", REPO, *LOGIN]
        for n in range(1, 27):
            argv += ["--issue", str(n)]
        code, _, err = self._main(argv)
        self.assertEqual(code, 2)
        self.assertIn("at most 25", err)
        self.assertEqual(deferral_store.read_deferrals([REPO]), set())

    def test_25_keys_are_accepted(self):
        argv = ["--repo", REPO, *LOGIN]
        for n in range(1, 26):
            argv += ["--issue", str(n)]
        code, _, _ = self._main(argv)
        self.assertEqual(code, 0)
        self.assertEqual(len(deferral_store.read_deferrals([REPO])), 25)

    def test_refuses_to_run_with_no_issue(self):
        code, _, _ = self._main(["--repo", REPO, *LOGIN])
        self.assertEqual(code, 2)
        self.assertEqual(deferral_store.read_deferrals([REPO]), set())

    def test_refuses_to_run_with_no_repo(self):
        code, _, _ = self._main(["--issue", "1", *LOGIN])
        self.assertEqual(code, 2)

    def test_refuses_to_run_with_no_operator_login(self):
        code, _, err = self._main(["--repo", REPO, "--issue", "2757"])
        self.assertEqual(code, 2)
        self.assertIn("--operator-login", err)
        self.assertEqual(deferral_store.read_deferrals([REPO]), set())

    def test_refuses_a_non_positive_issue_with_exit_2_and_writes_nothing(self):
        """Sticky-wicket fix (c), N5: the CLI calls `valid_key` itself, before
        any write, so one rejected key in a batch records nothing."""
        for bad in ("0", "-5"):
            with self.subTest(issue=bad):
                code, _, err = self._main(["--repo", REPO, "--issue", "2757", "--issue", bad, *LOGIN])
                self.assertEqual(code, 2)
                self.assertIn("invalid key", err)
                self.assertEqual(deferral_store.read_deferrals([REPO]), set())

    def test_the_login_is_stored_casefolded(self):
        code, _, _ = self._main(["--repo", REPO, "--issue", "2757", "--operator-login", "VitalHarmony"])
        self.assertEqual(code, 0)
        self.assertEqual(deferral_store.deferral_entry(REPO, 2757)["operator_login"], "vitalharmony")

    def test_refuses_a_repo_that_is_not_onboarded(self):
        for repo in ("hrse", "vitalharmony/not-a-project"):
            with self.subTest(repo=repo):
                code, _, err = self._main(["--repo", repo, "--issue", "1", *LOGIN])
                self.assertEqual(code, 2)
                self.assertIn("record_deferral:", err)
        self.assertEqual(list((self.base / "deferrals").glob("*.json"))
                         if (self.base / "deferrals").exists() else [], [])

    def test_records_under_the_canonical_key(self):
        for spelling in ("vitalharmony/HRSE", "https://github.com/vitalharmony/hrse"):
            with self.subTest(spelling=spelling):
                code, out, _ = self._main(["--repo", spelling, "--issue", "2757", *LOGIN])
                self.assertEqual(code, 0)
                self.assertIn(f"recorded deferral {REPO}#2757", out)
                self.assertEqual([p.name for p in (self.base / "deferrals").glob("*.json")],
                                 ["vitalharmony__hrse__2757.json"])


if __name__ == "__main__":
    unittest.main()
