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
        (Path(self.lane_slot) / "token").write_text("lanetok\n")  # harmonic-forge#1466: a lane slot carries its own token
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
        self.assertEqual(env.get("GH_TOKEN"), "lanetok")  # harmonic-forge#1466: the lane's own token
        self.assertNotIn("GITHUB_TOKEN", env)

    def test_unreadable_projects_token_still_strips_an_inherited_one(self):
        """Sticky-wicket PATCH, preclose pass 2 (d)."""
        (self.home / "vitalharmony-projects" / "token").unlink()
        other_owner = ["gh", "project", "item-list", "1", "--owner", "harmonicarchitect"]
        env = pt.projects_env(other_owner, self.env(GH_TOKEN="projtok", GITHUB_TOKEN="projtok"))
        self.assertEqual(env.get("GH_TOKEN"), "lanetok")  # harmonic-forge#1466: the lane's own token
        self.assertNotIn("GITHUB_TOKEN", env)

    def test_a_stripped_projects_token_is_replaced_by_the_lane_token(self):
        """Operator ruling 1a (post-verdict): with no GH_TOKEN gh can prefer the
        shared keyring's admin entry, so a strip pins the lane slot's own token."""
        (Path(self.lane_slot) / "token").write_text("lanefile\n")
        other_owner = ["gh", "project", "item-list", "1", "--owner", "harmonicarchitect"]
        env = pt.projects_env(other_owner, self.env(GH_TOKEN="projtok"))
        self.assertEqual(env.get("GH_TOKEN"), "lanefile")
        probe = pt.withhold_projects_token({"GH_CONFIG_DIR": self.lane_slot, "GH_TOKEN": "projtok"})
        self.assertEqual(probe.get("GH_TOKEN"), "lanefile")

    def test_unreadable_projects_token_pins_the_lane_token(self):
        (Path(self.lane_slot) / "token").write_text("lanefile\n")
        (self.home / "vitalharmony-projects" / "token").unlink()
        env = pt.projects_env(self.VH_PROJECT, self.env())
        self.assertEqual(env.get("GH_TOKEN"), "lanefile")
        self.assertNotIn("GITHUB_TOKEN", env)

    def test_a_slot_outside_the_root_is_never_pinned(self):
        elsewhere = Path(tempfile.mkdtemp()) / "vitalharmony-lane1"
        elsewhere.mkdir()
        (elsewhere / "token").write_text("foreign\n")
        self.addCleanup(lambda: ((elsewhere / "token").unlink(), elsewhere.rmdir()))
        # harmonic-forge#1466: in a lane, a lane-slot name outside the root cannot run gh.
        with self.assertRaises(pt.NoLaneToken):
            pt.withhold_projects_token({"GH_CONFIG_DIR": str(elsewhere), "GH_TOKEN": "projtok"})

    def test_bare_vitalharmony_slot_is_not_grantable(self):
        """Sticky-wicket PATCH, preclose pass 2 (b): its credential is the shared keyring."""
        bare = self.home / "vitalharmony"
        bare.mkdir()
        # harmonic-forge#1466: in a lane, the bare slot (the shared keyring) cannot run gh.
        with mock.patch.dict(os.environ, {"GH_CONFIG_DIR": str(bare)}), self.assertRaises(pt.NoLaneToken):
            pt.projects_env(self.VH_PROJECT, self.env(GH_CONFIG_DIR=str(bare)))

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
            "a lane the process is not in": {"LANE": "2"},
        }
        for name, extra in refused.items():
            with self.subTest(name):
                self.assertEqual(pt.projects_env(self.VH_PROJECT, self.env(**extra))["GH_TOKEN"], "lanetok")
        # harmonic-forge#1466: an allowlisted lane name with no directory has no token to run on.
        with self.assertRaises(pt.NoLaneToken):
            pt.projects_env(self.VH_PROJECT, self.env(GH_CONFIG_DIR=str(self.home / "vitalharmony-lane3")))
        # harmonic-forge#1466 preclose pass 1: no other vitalharmony-named slot can run gh in a lane.
        for name in ("vitalharmony-x", "vitalharmony-projects"):
            with self.subTest(slot=name), self.assertRaises(pt.NoLaneToken):
                pt.projects_env(self.VH_PROJECT, self.env(GH_CONFIG_DIR=str(self.home / name)))
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
            with self.subTest(name), self.assertRaises(pt.NoLaneToken):
                # Not granted (#1463); with no readable token for that slot, gh does not run (#1466).
                pt.projects_env(self.VH_PROJECT, self.env(GH_CONFIG_DIR=str(slot)))

    def test_a_process_slot_outside_the_accounts_root_is_refused(self):
        """harmonic-forge#1463 AC2: the slot must sit directly under the
        accounts root, even when it is the process's own GH_CONFIG_DIR."""
        elsewhere = Path(tempfile.mkdtemp()) / "vitalharmony-lane1"
        elsewhere.mkdir()
        self.addCleanup(lambda: elsewhere.rmdir())
        with mock.patch.dict(os.environ, {"GH_CONFIG_DIR": str(elsewhere)}), self.assertRaises(pt.NoLaneToken):
            pt.projects_env(self.VH_PROJECT, self.env(GH_CONFIG_DIR=str(elsewhere)))

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

    def test_1466_lane_rule(self):
        """harmonic-forge#1466: in a lane, a lane slot always leaves with its own
        token; an unset slot cannot run gh; another account's slot is unchanged;
        outside a lane nothing changes."""
        self.assertEqual(pt.withhold_projects_token({"GH_CONFIG_DIR": self.lane_slot})["GH_TOKEN"], "lanetok")
        with self.assertRaises(pt.NoLaneToken):
            pt.withhold_projects_token({})
        (Path(self.lane_slot) / "token").unlink()
        with self.assertRaises(pt.NoLaneToken):
            pt.withhold_projects_token({"GH_CONFIG_DIR": self.lane_slot})
        for name in ("vitalharmony-projects", "vitalharmony-x"):  # preclose pass 1 (cross-family)
            with self.subTest(slot=name), self.assertRaises(pt.NoLaneToken):
                pt.withhold_projects_token({"GH_CONFIG_DIR": str(self.home / name)})
        other = {"GH_CONFIG_DIR": str(self.home / "harmonicarchitect"), "GH_TOKEN": "hatok"}
        self.assertEqual(pt.withhold_projects_token(dict(other))["GH_TOKEN"], "hatok")
        with mock.patch.dict(os.environ, {"LANE": ""}):
            self.assertEqual(pt.withhold_projects_token({})  , {})

    def test_undecodable_token_file_falls_back_to_hosts_yml(self):
        """harmonic-forge#1466 sticky-wicket PATCH (c)."""
        slot = Path(self.lane_slot)
        (slot / "token").write_bytes(b"\xff\xfe\x00")
        (slot / "hosts.yml").write_text("github.com:\n    oauth_token: fromhosts\n")
        self.assertEqual(pt.slot_token("vitalharmony-lane1", str(self.home)), "fromhosts")

    def test_board_owner_reads_every_owner_form(self):
        self.assertEqual(pt.board_owner(["gh", "project", "view", "1", "--owner", "a"]), "a")
        self.assertEqual(pt.board_owner(["gh", "project", "view", "1", "--owner=b"]), "b")
        self.assertEqual(pt.board_owner(["gh", "api", "graphql", "-f", "owner=c"]), "c")
        self.assertIsNone(pt.board_owner(self.ITEM_EDIT))


if __name__ == "__main__":
    unittest.main()
