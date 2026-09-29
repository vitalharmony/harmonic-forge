#!/usr/bin/env python3
"""Tests for `manifest_identity.py` (harmonic-forge#804)."""
from __future__ import annotations

import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import manifest as mf  # noqa: E402
import manifest_identity as mi  # noqa: E402


def _manifest(text: str) -> Path:
    handle = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
    handle.write(textwrap.dedent(text))
    handle.close()
    return Path(handle.name)


class SlotDirTests(unittest.TestCase):
    def test_default_is_under_the_config_dir(self) -> None:
        with mock.patch.dict(os.environ):
            os.environ.pop("GH_ACCT_HOME", None)
            self.assertEqual(mi.slot_dir("vitalharmony"),
                             Path.home() / ".config" / "gh-accounts" / "vitalharmony")

    def test_gh_acct_home_overrides(self) -> None:
        with mock.patch.dict(os.environ, {"GH_ACCT_HOME": "/tmp/slots"}):
            self.assertEqual(mi.slot_dir("kn"), Path("/tmp/slots/kn"))


class AccountForTests(unittest.TestCase):
    def test_live_manifest_resolves_each_registered_repo(self) -> None:
        self.assertEqual(mi.account_for("vitalharmony/hrse"), "vitalharmony")
        self.assertEqual(mi.account_for("vitalharmony/harmonic-forge"), "vitalharmony")

    def test_a_mixed_case_repo_resolves(self) -> None:
        """harmonic-forge#800: LeasePAL-ML is the first mixed-case owner."""
        self.assertEqual(mi.account_for("LeasePAL-ML/LeasePAL-App-Prototype"),
                         "vitalharmony")

    def test_an_unknown_repo_raises_and_never_falls_back(self) -> None:
        with self.assertRaises(mf.ManifestError) as caught:
            mi.account_for("someone/else")
        self.assertIn("no [[project]] entry", str(caught.exception))
        self.assertIn("refusing to fall back", str(caught.exception))

    def test_a_registered_repo_without_an_account_has_its_own_message(self) -> None:
        project = mf.Project(name="x", prefix="X", repo="o/r")
        with mock.patch.object(mi, "by_repo", return_value={"o/r": project}):
            with self.assertRaises(mf.ManifestError) as caught:
                mi.account_for("o/r")
        self.assertIn("declares no `account`", str(caught.exception))
        self.assertNotIn("no [[project]] entry", str(caught.exception))


class ValidateTests(unittest.TestCase):
    def test_a_repo_without_an_account_is_refused_at_load(self) -> None:
        path = _manifest("""
            [[project]]
            name = "a"
            prefix = "A"
            repo = "o/a"
        """)
        with self.assertRaises(mf.ManifestError) as caught:
            mf.load(path)
        self.assertIn("declares a repo but no `account`", str(caught.exception))

    def test_a_projected_entry_with_no_repo_needs_no_account(self) -> None:
        path = _manifest("""
            [[project]]
            name = "a"
            prefix = "A"
        """)
        self.assertEqual(len(mf.load(path)), 1)

    def test_a_board_on_a_different_owner_is_allowed(self) -> None:
        """LeasePAL's board is owned by LeasePAL-ML but acted on as vitalharmony
        (verified live, harmonic-forge#800): the invariant is one acting
        identity per process, not that every owner equals the account."""
        path = _manifest("""
            [[project]]
            name = "a"
            prefix = "A"
            repo = "o/a"
            account = "vitalharmony"
            board_owner = "SomeoneElse"
            board_number = "1"
        """)
        self.assertEqual(mf.load(path)[0].board, ("SomeoneElse", "1"))


class ProjectForPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        (self.root / "co").mkdir()
        (self.root / "wt" / "co-lane2").mkdir(parents=True)
        self.manifest = _manifest(f"""
            [[project]]
            name = "a"
            prefix = "A"
            repo = "o/a"
            account = "acct"
            path = "{self.root / 'co'}"
            worktree_dir = "{self.root / 'wt'}"
        """)

    def test_the_checkout_and_a_subdirectory(self) -> None:
        (self.root / "co" / "sub").mkdir()
        self.assertEqual(mi.project_for_path(self.root / "co", self.manifest).name, "a")
        self.assertEqual(mi.project_for_path(self.root / "co" / "sub", self.manifest).name, "a")

    def test_a_lane_worktree_outside_the_checkouts_parent(self) -> None:
        """harmonic-forge's worktrees live in `worktree_dir`, not beside the checkout."""
        (self.root / "wt" / "co-lane2" / "deep").mkdir()
        self.assertEqual(
            mi.project_for_path(self.root / "wt" / "co-lane2" / "deep", self.manifest).name, "a")

    def test_an_unregistered_path_raises(self) -> None:
        with self.assertRaises(mf.ManifestError):
            mi.project_for_path(self.root, self.manifest)

    def test_the_live_lane_worktree_of_forge_resolves(self) -> None:
        lane2 = Path.home() / "Harmonic_Projects" / "harmonic-forge-lane2"
        if not lane2.exists():
            self.skipTest("no harmonic-forge-lane2 on this machine")
        self.assertEqual(mi.project_for_path(lane2).repo, "vitalharmony/harmonic-forge")


class SlotEnvTests(unittest.TestCase):
    def test_injects_the_slot_and_strips_both_tokens_without_mutating_os_environ(self) -> None:
        with mock.patch.dict(os.environ, {"GH_TOKEN": "t", "GITHUB_TOKEN": "u",
                                          "GH_CONFIG_DIR": "/elsewhere"}):
            env = mi.slot_env("vitalharmony/hrse")
            self.assertNotIn("GH_TOKEN", env)
            self.assertNotIn("GITHUB_TOKEN", env)
            self.assertEqual(env["GH_CONFIG_DIR"], str(mi.slot_dir("vitalharmony")))
            self.assertEqual(os.environ["GH_TOKEN"], "t")
            self.assertEqual(os.environ["GH_CONFIG_DIR"], "/elsewhere")

    def test_makes_no_gh_call(self) -> None:
        with mock.patch.object(mi, "_probe_login") as probe:
            mi.slot_env("vitalharmony/hrse")
        probe.assert_not_called()


class SlotEnvOrNoneTests(unittest.TestCase):
    def test_a_registered_repo_gets_its_slot(self) -> None:
        env = mi.slot_env_or_none("vitalharmony/hrse")
        self.assertEqual(env["GH_CONFIG_DIR"], str(mi.slot_dir("vitalharmony")))

    def test_an_unregistered_or_missing_repo_is_none_not_an_error(self) -> None:
        self.assertIsNone(mi.slot_env_or_none("someone/else"))
        self.assertIsNone(mi.slot_env_or_none(None))
        self.assertIsNone(mi.slot_env_or_none(""))


class ApplyProjectIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        mi._VERIFIED.clear()
        env = mock.patch.dict(os.environ, {"GH_TOKEN": "t", "GITHUB_TOKEN": "u"})
        env.start()
        self.addCleanup(env.stop)

    def test_sets_the_slot_and_clears_both_tokens(self) -> None:
        with mock.patch.object(mi, "_probe_login", return_value="vitalharmony"):
            self.assertEqual(mi.apply_project_identity("vitalharmony/hrse"), "vitalharmony")
        self.assertEqual(os.environ["GH_CONFIG_DIR"], str(mi.slot_dir("vitalharmony")))
        self.assertNotIn("GH_TOKEN", os.environ)
        self.assertNotIn("GITHUB_TOKEN", os.environ)

    def test_refuses_a_mismatched_login_with_the_documented_message(self) -> None:
        with mock.patch.object(mi, "_probe_login", return_value="someoneelse"):
            with self.assertRaises(SystemExit) as caught:
                mi.apply_project_identity("vitalharmony/hrse")
        self.assertEqual(
            str(caught.exception),
            "identity mismatch: vitalharmony/hrse needs vitalharmony, "
            "slot authenticates as someoneelse")

    def test_login_comparison_is_case_insensitive(self) -> None:
        with mock.patch.object(mi, "_probe_login", return_value="VitalHarmony"):
            mi.apply_project_identity("vitalharmony/hrse")

    def test_an_unauthenticated_slot_refuses_and_names_the_repair(self) -> None:
        with mock.patch.object(mi, "_probe_login", return_value=""):
            with self.assertRaises(SystemExit) as caught:
                mi.apply_project_identity("vitalharmony/hrse")
        self.assertIn("not authenticated", str(caught.exception))
        self.assertIn("gh-as --init vitalharmony", str(caught.exception))

    def test_an_unknown_repo_raises_before_any_probe(self) -> None:
        with mock.patch.object(mi, "_probe_login") as probe:
            with self.assertRaises(mf.ManifestError):
                mi.apply_project_identity("someone/else")
        probe.assert_not_called()

    def test_the_probe_runs_once_per_slot_per_process(self) -> None:
        with mock.patch.object(mi, "_probe_login", return_value="vitalharmony") as probe:
            mi.apply_project_identity("vitalharmony/hrse")
            mi.apply_project_identity("vitalharmony/harmonic-forge")
        self.assertEqual(probe.call_count, 1)

    def test_the_token_is_stripped_even_when_the_slot_is_cached(self) -> None:
        with mock.patch.object(mi, "_probe_login", return_value="vitalharmony"):
            mi.apply_project_identity("vitalharmony/hrse")
            os.environ["GH_TOKEN"] = "reintroduced"
            mi.apply_project_identity("vitalharmony/hrse")
        self.assertNotIn("GH_TOKEN", os.environ)

    def test_never_runs_gh_auth_switch(self) -> None:
        with mock.patch.object(mi.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="vitalharmony\n")
            mi.apply_project_identity("vitalharmony/hrse")
        for call in run.call_args_list:
            self.assertNotIn("switch", call.args[0])


if __name__ == "__main__":
    unittest.main()
