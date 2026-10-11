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


class RequirementLinesTests(unittest.TestCase):
    def test_grammar_keeps_markers_and_extras_and_drops_options_and_hashes(self):
        lines = l1_post.requirement_lines(
            "PyJWT[crypto]==2.15.0 ; python_version>'3' --hash=sha256:ab\n"
            "-r other.txt\n-e .\n-c constraints.txt\n--index-url x\n"
            "# a comment\nBar_Baz.Q==1.0  # trailing\n")
        self.assertEqual(lines, ["PyJWT[crypto]==2.15.0 ; python_version>'3'", "Bar_Baz.Q==1.0"])


class InstallStalenessTests(unittest.TestCase):
    def _venv(self, root: Path, installed: dict) -> Path:
        """A stand-in venv whose python is this interpreter, seeing only the
        given fake distributions (so the real `_VENV_CHECK` runs)."""
        site = root / "site"
        for name, version in installed.items():
            info = site / f"{name}-{version}.dist-info"
            info.mkdir(parents=True)
            (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
        install = root / "backend" / ".venv"
        (install / "bin").mkdir(parents=True)
        python = install / "bin" / "python"
        python.write_text(f"#!/bin/sh\nPYTHONPATH={site} exec {sys.executable} \"$@\"\n")
        python.chmod(0o755)
        return install

    def _stale(self, installed: dict, requirements: str) -> list:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            install = self._venv(root, installed)
            (root / "backend" / "requirements.txt").write_text(requirements)
            return l1_post.install_staleness("backend/.venv", install)

    def test_a_stale_pin_is_reported(self):
        self.assertEqual(self._stale({"pyjwt": "2.13.0"}, "PyJWT==2.15.0\n"),
                         ["PyJWT 2.13.0 (wants ==2.15.0)"])

    def test_equivalent_versions_and_inactive_markers_are_satisfied(self):
        self.assertEqual(self._stale({"demo": "1.0.0"},
                                     "demo==1.0\nwinonly==9 ; sys_platform == 'win32'\n"), [])

    def test_a_probe_that_cannot_run_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "backend" / ".venv" / "bin").mkdir(parents=True)
            (root / "backend" / "requirements.txt").write_text("x==1\n")
            with self.assertRaises(l1_post.StaleProbeError):
                l1_post.install_staleness("backend/.venv", root / "backend" / ".venv")
        with mock.patch.object(l1_post.subprocess, "run", side_effect=OSError("no npm")):
            with self.assertRaises(l1_post.StaleProbeError):
                l1_post.install_staleness("frontend/node_modules", Path("/x/frontend/node_modules"))

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


if __name__ == "__main__":
    unittest.main()
