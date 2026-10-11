"""harmonic-forge#970: the dependency and re-run guards in l1_post."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import l1_post  # noqa: E402


class RequirementPinsTests(unittest.TestCase):
    def test_grammar_markers_extras_hashes_and_skipped_lines(self):
        pins = l1_post.requirement_pins(
            "PyJWT[crypto]==2.15.0 ; python_version>'3' --hash=sha256:ab\n"
            "-r other.txt\n-e .\n-c constraints.txt\n--index-url x\n"
            "loose>=1.0\n# a comment\nBar_Baz.Q==1.0  # trailing\n")
        self.assertEqual(pins, {"pyjwt": "2.15.0", "bar-baz-q": "1.0"})


class InstallStalenessTests(unittest.TestCase):
    def _venv(self, root: Path, installed: dict) -> Path:
        install = root / "backend" / ".venv"
        (install / "bin").mkdir(parents=True)
        python = install / "bin" / "python"
        python.write_text("#!/bin/sh\necho '" + json.dumps(installed) + "'\n")
        python.chmod(0o755)
        return install

    def test_a_stale_pin_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            install = self._venv(root, {"pyjwt": "2.13.0", "anyio": "4.14.2"})
            (root / "backend" / "requirements.txt").write_text("PyJWT==2.15.0\nanyio==4.14.2\n")
            stale = l1_post.install_staleness("backend/.venv", install)
        self.assertEqual(stale, ["pyjwt 2.13.0!=2.15.0"])

    def test_a_satisfied_venv_is_clean(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            install = self._venv(root, {"pyjwt": "2.15.0"})
            (root / "backend" / "requirements.txt").write_text("PyJWT==2.15.0\n")
            self.assertEqual(l1_post.install_staleness("backend/.venv", install), [])

    def test_npm_missing_is_reported_and_extraneous_is_not(self):
        out = json.dumps({"dependencies": {
            "@sentry/react": {"missing": True}, "react": {"version": "18"},
            "leftover": {"extraneous": True}}})
        with mock.patch.object(l1_post.subprocess, "run",
                               return_value=mock.Mock(stdout=out, returncode=1)):
            stale = l1_post.install_staleness("frontend/node_modules", Path("/x/frontend/node_modules"))
        self.assertEqual(stale, ["@sentry/react (missing)"])

    def test_provision_refuses_a_stale_install_instead_of_linking(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "src"
            scratch = Path(d) / "scratch"
            (root / "backend" / ".venv").mkdir(parents=True)
            (scratch / "backend").mkdir(parents=True)
            with mock.patch.object(l1_post, "_manifest_differences", return_value=[]), \
                 mock.patch.object(l1_post, "install_staleness", return_value=["pyjwt 1!=2"]), \
                 mock.patch.object(l1_post, "fail", side_effect=SystemExit) as failed:
                with self.assertRaises(SystemExit):
                    l1_post._provision_scratch_dependency(root, scratch, "backend/.venv")
            self.assertIn("does not satisfy its manifest", failed.call_args[0][0])
            self.assertFalse((scratch / "backend" / ".venv").exists())


class UnchangedRerunTests(unittest.TestCase):
    def _attempt(self, outcome, fp):
        return {"ts": "2026-10-11T03:00:00Z",
                "attrs": {"outcome": outcome, "install_fingerprint_sha": fp}}

    def test_an_unchanged_rerun_after_a_failed_check_is_refused(self):
        with mock.patch.object(l1_post, "last_attempt", return_value=self._attempt("refused-check", "f1")), \
             mock.patch.object(l1_post, "fail", side_effect=SystemExit) as failed:
            with self.assertRaises(SystemExit):
                l1_post.refuse_unchanged_rerun("o/r", 1, "a" * 40, "f1")
        self.assertIn("failed its full check", failed.call_args[0][0])

    def test_a_changed_install_or_a_posted_attempt_or_no_record_is_allowed(self):
        for prev, fp in ((self._attempt("refused-check", "f1"), "f2"),
                         (self._attempt("posted", "f1"), "f1"),
                         (None, "f1"),
                         (self._attempt("refused-check", None), "f1")):
            with mock.patch.object(l1_post, "last_attempt", return_value=prev), \
                 mock.patch.object(l1_post, "fail", side_effect=SystemExit):
                l1_post.refuse_unchanged_rerun("o/r", 1, "a" * 40, fp)  # no raise

    def test_the_fingerprint_changes_with_the_install(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            nm = root / "frontend" / "node_modules"
            nm.mkdir(parents=True)
            (root / "frontend" / "package.json").write_text("{}")
            (nm / ".package-lock.json").write_text("a")
            before = l1_post.install_fingerprint(root)
            (nm / ".package-lock.json").write_text("b")
            self.assertNotEqual(before, l1_post.install_fingerprint(root))


if __name__ == "__main__":
    unittest.main()
