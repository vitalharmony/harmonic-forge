#!/usr/bin/env python3
"""Tests for forge-onboard's `gh shim` check (harmonic-forge#960). HOME, PATH
and the platform checkout are temp dirs; the real ~/.local/bin is never read."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge_onboard as fo  # noqa: E402


class GhShimCheck(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.home = root / "home"
        self.bin = self.home / ".local" / "bin"
        self.bin.mkdir(parents=True)
        self.platform = root / "forge"
        (self.platform / "tools" / "gh").mkdir(parents=True)
        self.shim = self.platform / "tools" / "gh" / "gh_shim"
        self.shim.write_text("#!/bin/sh\n")
        self.shim.chmod(0o755)
        self.usr = root / "usr"
        self.usr.mkdir()
        real = self.usr / "gh"
        real.write_text("#!/bin/sh\n")
        real.chmod(0o755)
        self.path = f"{self.bin}:{self.usr}"
        # AC3: every FAIL detail carries this literal command (resolved path).
        self.cmd = f"bash {self.platform}/tools/gh/install_gh_shim.sh"
        for p in (mock.patch.object(fo, "platform_source", return_value=self.platform),
                  mock.patch.object(Path, "home", return_value=self.home)):
            p.start()
            self.addCleanup(p.stop)

    def run_check(self, path=None):
        with mock.patch.dict(os.environ, {"PATH": path or self.path}):
            return fo.check_gh_shim(None)

    def test_a_link_to_a_missing_shim_names_the_missing_shim(self):  # preclose pass 1
        (self.bin / "gh").symlink_to(self.shim)
        self.shim.unlink()
        check = self.run_check()
        self.assertEqual(check.status, "FAIL")
        self.assertIn("missing or not executable", check.detail)
        self.assertNotIn("first on PATH", check.detail)
        self.assertIn(self.cmd, check.detail)

    def test_missing_fails_and_names_the_install_command(self):  # TC1, TC3
        check = self.run_check()
        self.assertEqual(check.status, fo.FAIL)
        self.assertIn(self.cmd, check.detail)
        self.assertFalse((self.bin / "gh").exists())  # AC4: nothing written

    def test_a_real_file_fails(self):  # TC2
        (self.bin / "gh").write_text("#!/bin/sh\n")
        (self.bin / "gh").chmod(0o755)
        check = self.run_check()
        self.assertEqual(check.status, fo.FAIL)
        self.assertIn("real file", check.detail)
        self.assertIn(self.cmd, check.detail)

    def test_a_link_elsewhere_fails(self):  # TC2
        (self.bin / "gh").symlink_to(self.usr / "gh")
        check = self.run_check()
        self.assertEqual(check.status, fo.FAIL)
        self.assertIn(f"rm {self.bin / 'gh'}", check.detail)  # the installer won't overwrite it
        self.assertIn(self.cmd, check.detail)

    def test_not_first_on_path_fails(self):  # TC2
        (self.bin / "gh").symlink_to(self.shim)
        check = self.run_check(f"{self.usr}:{self.bin}")
        self.assertEqual(check.status, fo.FAIL)
        self.assertIn("first on PATH", check.detail)
        self.assertIn(self.cmd, check.detail)

    def test_linked_and_first_is_ok(self):  # TC5
        (self.bin / "gh").symlink_to(self.shim)
        self.assertEqual(self.run_check().status, fo.OK)

    def test_a_dotdot_path_spelling_is_ok(self):  # resolved, not text
        (self.bin / "gh").symlink_to(self.shim)
        (self.home / ".local" / "share").mkdir()
        self.assertEqual(self.run_check(f"{self.home}/.local/share/../bin:{self.usr}").status, fo.OK)

    def test_registered_in_checks(self):  # AC1
        self.assertIn(fo.check_gh_shim, fo.CHECKS)


if __name__ == "__main__":
    unittest.main()
