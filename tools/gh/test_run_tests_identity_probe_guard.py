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

    def test_a_lane_slot_answers_its_account(self) -> None:
        """harmonic-forge#1470: in a lane, apply_project_identity probes the lane
        slot, which authenticates as its account, not as the slot's name."""
        def trap(argv, *a, **k):
            raise AssertionError(f"the stub did not intercept the probe argv: {argv!r}")

        with mock.patch("subprocess.run", side_effect=trap):
            with run_tests.hermetic_identity_probe():
                with mock.patch.dict(os.environ, {"GH_CONFIG_DIR": "/slots/someacct-lane3"}):
                    login = mi._probe_login()
        self.assertEqual(login, "someacct")

    def test_a_missing_default_lane_slot_gets_a_placeholder_token(self) -> None:
        """harmonic-forge#1470: on a runner with no real lane slots, a lane
        entrypoint test finds a placeholder token; a test's own slot home does not."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(Path, "home", return_value=Path(tmp)), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GH_ACCT_HOME", None)
            with run_tests.hermetic_lane_token():
                self.assertEqual(mi._hosts_file_token(mi.slot_dir("vitalharmony-lane2")),
                                 run_tests.HERMETIC_LANE_TOKEN)
                self.assertEqual(mi._hosts_file_token(mi.slot_dir("vitalharmony")), "")
                os.environ["GH_ACCT_HOME"] = str(Path(tmp) / "own")
                self.assertEqual(mi._hosts_file_token(mi.slot_dir("vitalharmony-lane2")), "")
            self.assertEqual(mi._hosts_file_token(Path(tmp) / ".config" / "gh-accounts"
                                                  / "vitalharmony-lane2"), "")

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
