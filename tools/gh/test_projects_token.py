#!/usr/bin/env python3
"""Tests for projects_token (harmonic-forge#1396): a lane's board call on a
vitalharmony board carries the projects slot's token; nothing else does."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import projects_token as pt  # noqa: E402


class ProjectsEnvTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        (self.home / "vitalharmony-projects").mkdir()
        (self.home / "vitalharmony-projects" / "token").write_text("projtok\n")
        self.lane_slot = str(self.home / "vitalharmony-lane1")
        Path(self.lane_slot).mkdir()  # harmonic-forge#1463: the slot must exist
        # The process's own identity: lane, slot and accounts root (harmonic-forge#1463).
        patcher = mock.patch.dict(os.environ, {"LANE": "1", "GH_CONFIG_DIR": self.lane_slot,
                                               "GH_ACCT_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def env(self, **extra) -> dict[str, str]:
        base = {"LANE": "1", "GH_ACCT_HOME": str(self.home), "GH_CONFIG_DIR": self.lane_slot,
                "GH_TOKEN": "lanetok", "GITHUB_TOKEN": "stray"}
        base.update(extra)
        return base

    VH_GRAPHQL = ["gh", "api", "graphql", "-f", "query=query { x }", "-F", "owner=vitalharmony"]
    VH_PROJECT = ["gh", "project", "field-list", "1", "--owner", "vitalharmony"]
    ITEM_EDIT = ["gh", "project", "item-edit", "--project-id", "PVT_1", "--id", "I_1"]

    def test_a_lane_vitalharmony_board_call_carries_the_projects_token(self):
        for cmd in (self.VH_GRAPHQL, self.VH_PROJECT, self.ITEM_EDIT):
            with self.subTest(cmd[1:3]):
                env = pt.projects_env(cmd, self.env())
                self.assertEqual(env["GH_TOKEN"], "projtok")
                self.assertNotIn("GITHUB_TOKEN", env)

    def test_another_accounts_board_keeps_the_callers_token(self):
        cases = {
            "owner names another account": (["gh", "project", "item-list", "1", "--owner", "harmonicarchitect"], {}),
            "gh-as selected another account's slot": (self.ITEM_EDIT,
                                                      {"GH_CONFIG_DIR": str(self.home / "harmonicarchitect")}),
        }
        for name, (cmd, extra) in cases.items():
            with self.subTest(name):
                self.assertEqual(pt.projects_env(cmd, self.env(**extra))["GH_TOKEN"], "lanetok")

    def test_no_lane_or_no_projects_token_changes_nothing(self):
        self.assertEqual(pt.projects_env(self.VH_PROJECT, self.env(LANE=""))["GH_TOKEN"], "lanetok")
        (self.home / "vitalharmony-projects" / "token").unlink()
        # Sticky-wicket PATCH: an unreadable projects token fails closed for a
        # lane slot; gh then uses that lane slot's own stored credential.
        env = pt.projects_env(self.VH_PROJECT, self.env())
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotIn("GITHUB_TOKEN", env)

    def test_unreadable_projects_token_still_strips_an_inherited_one(self):
        """Sticky-wicket PATCH, preclose pass 2 (d)."""
        (self.home / "vitalharmony-projects" / "token").unlink()
        other_owner = ["gh", "project", "item-list", "1", "--owner", "harmonicarchitect"]
        env = pt.projects_env(other_owner, self.env(GH_TOKEN="projtok", GITHUB_TOKEN="projtok"))
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotIn("GITHUB_TOKEN", env)

    def test_bare_vitalharmony_slot_is_not_grantable(self):
        """Sticky-wicket PATCH, preclose pass 2 (b): its credential is the shared keyring."""
        bare = self.home / "vitalharmony"
        bare.mkdir()
        with mock.patch.dict(os.environ, {"GH_CONFIG_DIR": str(bare)}):
            env = pt.projects_env(self.VH_PROJECT, self.env(GH_CONFIG_DIR=str(bare)))
        self.assertEqual(env["GH_TOKEN"], "lanetok")

    def test_inherited_projects_token_is_stripped_when_not_granted(self):
        """harmonic-forge#1463 AC1: GH_TOKEN and GITHUB_TOKEN, every non-granting path."""
        other_owner = ["gh", "project", "item-list", "1", "--owner", "harmonicarchitect"]
        paths = {
            "other owner": (other_owner, {}),
            "harmonicarchitect slot": (self.ITEM_EDIT, {"GH_CONFIG_DIR": str(self.home / "harmonicarchitect")}),
            "no lane": (self.VH_PROJECT, {"LANE": ""}),
        }
        for key in ("GH_TOKEN", "GITHUB_TOKEN"):
            for name, (cmd, extra) in paths.items():
                with self.subTest(key=key, path=name):
                    env = pt.projects_env(cmd, self.env(**{key: "projtok", **extra}))
                    self.assertNotEqual(env.get(key), "projtok")

    def test_only_an_allowlisted_existing_slot_in_the_process_lane_is_granted(self):
        """harmonic-forge#1463 AC2."""
        (self.home / "vitalharmony-x").mkdir()
        refused = {
            "unregistered vitalharmony- slot": {"GH_CONFIG_DIR": str(self.home / "vitalharmony-x")},
            "allowlisted name, no directory": {"GH_CONFIG_DIR": str(self.home / "vitalharmony-lane3")},
            "the projects slot itself": {"GH_CONFIG_DIR": str(self.home / "vitalharmony-projects")},
            "a lane the process is not in": {"LANE": "2"},
        }
        for name, extra in refused.items():
            with self.subTest(name):
                self.assertEqual(pt.projects_env(self.VH_PROJECT, self.env(**extra))["GH_TOKEN"], "lanetok")
        # The process has no lane; it keeps its accounts root, so the result
        # does not depend on the machine's ~/.config/gh-accounts.
        with mock.patch.dict(os.environ, {"GH_ACCT_HOME": str(self.home)}, clear=True):
            self.assertEqual(pt.projects_env(self.VH_PROJECT, self.env())["GH_TOKEN"], "lanetok")

    def test_a_slot_other_than_the_process_own_is_refused(self):
        """harmonic-forge#1463 AC2, preclose pass 1: another registered slot, or
        an allowlisted name outside the accounts root, gets no token."""
        (self.home / "vitalharmony-lane3").mkdir()
        elsewhere = Path(tempfile.mkdtemp()) / "vitalharmony-lane1"
        elsewhere.mkdir()
        self.addCleanup(lambda: elsewhere.rmdir())
        for name, slot in {"another lane slot": self.home / "vitalharmony-lane3",
                           "allowlisted name outside the root": elsewhere}.items():
            with self.subTest(name):
                self.assertEqual(pt.projects_env(self.VH_PROJECT, self.env(GH_CONFIG_DIR=str(slot)))["GH_TOKEN"],
                                 "lanetok")

    def test_a_process_slot_outside_the_accounts_root_is_refused(self):
        """harmonic-forge#1463 AC2: the slot must sit directly under the
        accounts root, even when it is the process's own GH_CONFIG_DIR."""
        elsewhere = Path(tempfile.mkdtemp()) / "vitalharmony-lane1"
        elsewhere.mkdir()
        self.addCleanup(lambda: elsewhere.rmdir())
        with mock.patch.dict(os.environ, {"GH_CONFIG_DIR": str(elsewhere)}):
            env = pt.projects_env(self.VH_PROJECT, self.env(GH_CONFIG_DIR=str(elsewhere)))
        self.assertEqual(env["GH_TOKEN"], "lanetok")

    def test_strip_reads_the_process_accounts_root_not_the_base(self):
        """harmonic-forge#1463 AC1, preclose pass 1: a base pointing GH_ACCT_HOME
        at an empty directory still loses the inherited token."""
        empty = tempfile.mkdtemp()
        other_owner = ["gh", "project", "item-list", "1", "--owner", "harmonicarchitect"]
        env = pt.projects_env(other_owner, self.env(GH_TOKEN="projtok", GH_ACCT_HOME=empty))
        self.assertNotEqual(env.get("GH_TOKEN"), "projtok")

    def test_tilde_accounts_root_is_expanded(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.home.parent)}):
            tilde = "~/" + self.home.name
            with mock.patch.dict(os.environ, {"GH_ACCT_HOME": tilde}):
                self.assertEqual(pt.projects_env(self.VH_PROJECT, self.env(GH_ACCT_HOME=tilde))["GH_TOKEN"], "projtok")

    def test_repo_hygiene_lane_base_is_still_granted(self):
        """harmonic-forge#1463 AC2: repo_hygiene passes its own slot_env_or_none base."""
        base = {"LANE": "1", "GH_ACCT_HOME": str(self.home), "GH_CONFIG_DIR": self.lane_slot, "GH_TOKEN": "lanetok"}
        self.assertEqual(pt.projects_env(self.VH_PROJECT, base)["GH_TOKEN"], "projtok")

    def test_board_owner_reads_every_owner_form(self):
        self.assertEqual(pt.board_owner(["gh", "project", "view", "1", "--owner", "a"]), "a")
        self.assertEqual(pt.board_owner(["gh", "project", "view", "1", "--owner=b"]), "b")
        self.assertEqual(pt.board_owner(["gh", "api", "graphql", "-f", "owner=c"]), "c")
        self.assertIsNone(pt.board_owner(self.ITEM_EDIT))


if __name__ == "__main__":
    unittest.main()
