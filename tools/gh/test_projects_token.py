#!/usr/bin/env python3
"""Tests for projects_token (harmonic-forge#1396): a lane's board call on a
vitalharmony board carries the projects slot's token; nothing else does."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

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
        self.assertEqual(pt.projects_env(self.VH_PROJECT, self.env())["GH_TOKEN"], "lanetok")

    def test_board_owner_reads_every_owner_form(self):
        self.assertEqual(pt.board_owner(["gh", "project", "view", "1", "--owner", "a"]), "a")
        self.assertEqual(pt.board_owner(["gh", "project", "view", "1", "--owner=b"]), "b")
        self.assertEqual(pt.board_owner(["gh", "api", "graphql", "-f", "owner=c"]), "c")
        self.assertIsNone(pt.board_owner(self.ITEM_EDIT))


if __name__ == "__main__":
    unittest.main()
