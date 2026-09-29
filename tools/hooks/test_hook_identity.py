#!/usr/bin/env python3
"""Tests for `hook_identity.py` and the hooks that use it (harmonic-forge#804).

Every hook here fails open, so the properties that matter are: a registered repo's
`gh` call carries that repo's slot and no inherited token; an unregistered repo
(or no repo) changes nothing; and no hook mutates `os.environ`.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "onboard"))

import batch_preflight as bp  # noqa: E402
import block_data_migration_close as dmc  # noqa: E402
import block_missing_preclose_inspection as mpi  # noqa: E402
import block_undetermined_phase_close as upc  # noqa: E402
import hook_identity as hi  # noqa: E402
import manifest_identity as mi  # noqa: E402
import report_red_main as rrm  # noqa: E402

REGISTERED = "vitalharmony/hrse"


def _ok(stdout: str = "x") -> mock.Mock:
    return mock.Mock(returncode=0, stdout=stdout, stderr="")


class HookIdentityTests(unittest.TestCase):
    def test_a_registered_repo_gets_its_slot_and_no_tokens(self) -> None:
        with mock.patch.dict(os.environ, {"GH_TOKEN": "t", "GITHUB_TOKEN": "u"}):
            env = hi.slot_env(REGISTERED)
        self.assertEqual(env["GH_CONFIG_DIR"], str(mi.slot_dir("vitalharmony")))
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotIn("GITHUB_TOKEN", env)

    def test_an_unregistered_or_missing_repo_inherits(self) -> None:
        self.assertIsNone(hi.slot_env("someone/else"))
        self.assertIsNone(hi.slot_env(None))

    def test_slot_env_does_not_mutate_os_environ(self) -> None:
        with mock.patch.dict(os.environ, {"GH_TOKEN": "t"}, clear=False):
            before = dict(os.environ)
            hi.slot_env(REGISTERED)
            self.assertEqual(dict(os.environ), before)

    def test_it_makes_no_probe(self) -> None:
        with mock.patch.object(mi, "_probe_login") as probe:
            hi.slot_env(REGISTERED)
        probe.assert_not_called()

    def test_repo_from_checkout_resolves_a_registered_checkout(self) -> None:
        forge = Path.home() / "harmonic-forge"
        if not forge.exists():
            self.skipTest("no ~/harmonic-forge on this machine")
        self.assertEqual(hi.repo_from_checkout(forge), "vitalharmony/harmonic-forge")

    def test_repo_from_checkout_is_none_for_an_unregistered_path(self) -> None:
        self.assertIsNone(hi.repo_from_checkout("/tmp"))


class EachHookPassesItsReposSlot(unittest.TestCase):
    """One assertion per hook: the `gh` subprocess gets the slot for `repo`."""

    def _env_used(self, call) -> dict | None:
        with mock.patch("subprocess.run", return_value=_ok()) as run, \
             mock.patch("shutil.which", return_value="/usr/bin/gh"):
            call()
        return run.call_args.kwargs.get("env")

    def test_the_three_close_gating_hooks(self) -> None:
        for module in (mpi, upc, dmc):
            with self.subTest(hook=module.__name__):
                env = self._env_used(lambda m=module: m._gh("api", "x", repo=REGISTERED))
                self.assertEqual(env["GH_CONFIG_DIR"], str(mi.slot_dir("vitalharmony")))

    def test_without_a_repo_the_three_hooks_inherit(self) -> None:
        for module in (mpi, upc, dmc):
            with self.subTest(hook=module.__name__):
                self.assertIsNone(self._env_used(lambda m=module: m._gh("api", "x")))

    def test_labels_for_threads_the_repo_through(self) -> None:
        for module in (mpi, upc, dmc):
            with self.subTest(hook=module.__name__):
                env = self._env_used(lambda m=module: m.labels_for(REGISTERED, "5"))
                self.assertEqual(env["GH_CONFIG_DIR"], str(mi.slot_dir("vitalharmony")))

    def test_batch_preflight(self) -> None:
        with mock.patch("subprocess.run", return_value=_ok("{}")) as run:
            bp._gh("issue", "view", "1", repo=REGISTERED)
        self.assertEqual(run.call_args.kwargs["env"]["GH_CONFIG_DIR"],
                         str(mi.slot_dir("vitalharmony")))

    def test_report_red_main_gives_each_repo_its_own_env(self) -> None:
        seen = []
        with mock.patch.object(rrm, "_run", side_effect=lambda cmd, env=None: (seen.append(env), (1, ""))[1]):
            rrm._main_sha(REGISTERED)
            rrm._main_sha("someone/else")
        self.assertEqual(seen[0]["GH_CONFIG_DIR"], str(mi.slot_dir("vitalharmony")))
        self.assertIsNone(seen[1])


class NoHookMutatesTheEnvironment(unittest.TestCase):
    def test_gh_calls_leave_os_environ_untouched(self) -> None:
        with mock.patch.dict(os.environ, {"GH_TOKEN": "t"}):
            before = dict(os.environ)
            with mock.patch("subprocess.run", return_value=_ok()), \
                 mock.patch("shutil.which", return_value="/usr/bin/gh"):
                mpi._gh("api", "x", repo=REGISTERED)
                upc._gh("api", "x", repo=REGISTERED)
                dmc._gh("api", "x", repo=REGISTERED)
            self.assertEqual(dict(os.environ), before)


if __name__ == "__main__":
    unittest.main()
