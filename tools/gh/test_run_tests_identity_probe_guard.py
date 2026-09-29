#!/usr/bin/env python3
"""Guard for `run_tests.hermetic_identity_probe` (harmonic-forge#804).

The stub answers ONE hardcoded argv. If `manifest_identity._probe_login` ever changes its
command, the stub silently stops matching and every entrypoint test goes back to a real
`gh api user` -- green on a machine with a live slot, red on a CI runner, with the cause in
a file the diff never touched. This test is the alarm: it drives the real probe inside the
stub with the real `subprocess.run` booby-trapped, so a non-matching argv fails loudly here.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "onboard"))

import manifest_identity as mi  # noqa: E402
import run_tests  # noqa: E402


class HermeticIdentityProbeGuard(unittest.TestCase):
    def test_the_stub_answers_the_argv_the_real_probe_emits(self) -> None:
        def trap(argv, *a, **k):
            raise AssertionError(f"the stub did not intercept the probe argv: {argv!r}")

        with mock.patch("subprocess.run", side_effect=trap):
            with run_tests.hermetic_identity_probe():
                with mock.patch.dict(os.environ, {"GH_CONFIG_DIR": "/slots/someacct"}):
                    login = mi._probe_login()
        self.assertEqual(login, "someacct")

    def test_other_commands_pass_through_untouched(self) -> None:
        sentinel = subprocess.CompletedProcess(["git", "--version"], 0, stdout="v", stderr="")
        with mock.patch("subprocess.run", return_value=sentinel):
            with run_tests.hermetic_identity_probe():
                self.assertIs(subprocess.run(["git", "--version"]), sentinel)

    def test_the_stub_is_removed_afterwards(self) -> None:
        before = subprocess.run
        with run_tests.hermetic_identity_probe():
            self.assertIsNot(subprocess.run, before)
        self.assertIs(subprocess.run, before)


if __name__ == "__main__":
    unittest.main()
