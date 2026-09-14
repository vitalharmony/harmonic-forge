"""harmonic-forge#650 -- the gh_shim executable.

Imported as a module (it is a valid, `#!`-prefixed Python file with an
`if __name__ == "__main__"` guard) so its internals can be exercised without
actually exec'ing a real `gh`.
"""
import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SHIM_PATH = Path(__file__).parent / "gh_shim"
_INSTALLER = Path(__file__).parent / "install_gh_shim.sh"


def _load_shim():
    # gh_shim has no .py extension (it's installed as a bare executable), so
    # spec_from_file_location can't infer a loader from the suffix -- supply
    # the source loader explicitly.
    loader = importlib.machinery.SourceFileLoader("gh_shim_module", str(_SHIM_PATH))
    spec = importlib.util.spec_from_file_location("gh_shim_module", _SHIM_PATH, loader=loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class RealGhResolution(unittest.TestCase):
    def test_skips_itself_when_shim_is_first_on_path(self):
        """Simulate PATH listing a directory containing a copy of this same
        shim before a directory containing the real gh -- resolution must
        skip its own realpath and find the real binary."""
        shim = _load_shim()
        with mock.patch("os.path.isfile", side_effect=lambda p: p.endswith("gh")), \
             mock.patch("os.path.realpath") as fake_realpath, \
             mock.patch.dict("os.environ", {"PATH": "/fake/shim/dir:/usr/bin"}):
            def realpath_side_effect(path):
                if path == "/fake/shim/dir/gh":
                    return shim._self_realpath()
                if path == "/usr/bin/gh":
                    return "/usr/bin/gh"
                return path
            fake_realpath.side_effect = realpath_side_effect
            resolved = shim._resolve_real_gh()
        self.assertEqual(resolved, "/usr/bin/gh")

    def test_falls_back_to_usr_bin_gh_when_nothing_found(self):
        shim = _load_shim()
        with mock.patch("os.path.isfile", return_value=False), \
             mock.patch.dict("os.environ", {"PATH": "/nowhere"}):
            self.assertEqual(shim._resolve_real_gh(), "/usr/bin/gh")


class Refusal(unittest.TestCase):
    def _run_main_with(self, argv, scan_reason=None, override=False, budget_result=(4000, 1.0)):
        shim = _load_shim()
        fake_patterns = mock.Mock()
        fake_patterns.scan_reason.return_value = scan_reason
        fake_patterns.consume_override.return_value = override
        fake_patterns.budget.return_value = budget_result
        with mock.patch.object(sys, "argv", ["gh", *argv]), \
             mock.patch.object(shim, "_resolve_real_gh", return_value="/usr/bin/gh"), \
             mock.patch.dict(sys.modules, {"gh_scan_patterns": fake_patterns}), \
             mock.patch("os.execv") as fake_execv:
            try:
                shim.main()
            except SystemExit as exc:
                return exc.code, fake_execv
            return None, fake_execv

    def test_scan_pattern_refuses_with_exit_3(self):
        code, execv = self._run_main_with(["issue", "list"], scan_reason="scan!", override=False)
        self.assertEqual(code, 3)
        execv.assert_not_called()

    def test_override_present_lets_a_scan_through(self):
        code, execv = self._run_main_with(["issue", "list"], scan_reason="scan!", override=True)
        self.assertIsNone(code)
        execv.assert_called_once()

    def test_low_budget_refuses_with_exit_3(self):
        code, execv = self._run_main_with(["pr", "view", "5"], scan_reason=None,
                                           budget_result=(500, 1.0))
        self.assertEqual(code, 3)
        execv.assert_not_called()

    def test_healthy_budget_and_no_scan_execs_real_gh(self):
        code, execv = self._run_main_with(["pr", "view", "5"], scan_reason=None,
                                           budget_result=(4000, 1.0))
        self.assertIsNone(code)
        execv.assert_called_once()

    def test_auth_commands_skip_the_budget_check(self):
        shim = _load_shim()
        fake_patterns = mock.Mock()
        fake_patterns.scan_reason.return_value = None
        fake_patterns.consume_override.return_value = False
        with mock.patch.object(sys, "argv", ["gh", "auth", "status"]), \
             mock.patch.object(shim, "_resolve_real_gh", return_value="/usr/bin/gh"), \
             mock.patch.dict(sys.modules, {"gh_scan_patterns": fake_patterns}), \
             mock.patch("os.execv") as fake_execv:
            shim.main()
        fake_patterns.budget.assert_not_called()
        fake_execv.assert_called_once()

    def test_internal_exception_fails_open(self):
        """A bug in the shim must never brick `gh` entirely."""
        shim = _load_shim()
        fake_patterns = mock.Mock()
        fake_patterns.scan_reason.side_effect = RuntimeError("boom")
        with mock.patch.object(sys, "argv", ["gh", "pr", "view", "5"]), \
             mock.patch.object(shim, "_resolve_real_gh", return_value="/usr/bin/gh"), \
             mock.patch.dict(sys.modules, {"gh_scan_patterns": fake_patterns}), \
             mock.patch("os.execv") as fake_execv:
            shim.main()
        fake_execv.assert_called_once()


class Installer(unittest.TestCase):
    """Exercises install_gh_shim.sh's idempotent-link logic against a
    throwaway temp HOME/PATH -- never the real operator environment."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.home = Path(self._tmpdir.name)
        self.local_bin = self.home / ".local" / "bin"

    def tearDown(self):
        self._tmpdir.cleanup()

    def _run_installer(self):
        env = dict(os.environ)
        env["HOME"] = str(self.home)
        # command -v gh must resolve through the freshly-populated PATH.
        env["PATH"] = f"{self.local_bin}:{env.get('PATH', '')}"
        return subprocess.run(
            ["bash", str(_INSTALLER)], capture_output=True, text=True, env=env,
        )

    def test_creates_local_bin_and_links_when_absent(self):
        result = self._run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        target = self.local_bin / "gh"
        self.assertTrue(target.is_symlink())
        self.assertEqual(os.path.realpath(target), os.path.realpath(_SHIM_PATH))

    def test_idempotent_when_rerun(self):
        first = self._run_installer()
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self._run_installer()
        self.assertEqual(second.returncode, 0, second.stderr)

    def test_refuses_to_overwrite_a_real_file(self):
        self.local_bin.mkdir(parents=True)
        real_gh = self.local_bin / "gh"
        real_gh.write_text("#!/bin/sh\necho not the shim\n")
        real_gh.chmod(0o755)
        result = self._run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists", result.stderr)
        # Must not have been touched.
        self.assertFalse(real_gh.is_symlink())

    def test_refuses_to_overwrite_a_symlink_to_something_else(self):
        self.local_bin.mkdir(parents=True)
        elsewhere = self.home / "some_other_gh"
        elsewhere.write_text("#!/bin/sh\n")
        elsewhere.chmod(0o755)
        (self.local_bin / "gh").symlink_to(elsewhere)
        result = self._run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not this repo's gh_shim", result.stderr)


if __name__ == "__main__":
    unittest.main()
