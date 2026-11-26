#!/usr/bin/env python3
"""Tests for `manifest_slots.py`: per-account gh slot modes from `projects.toml`'s
`[accounts]` table (harmonic-forge#1528). Every case uses a scratch manifest and a
scratch slot root; none reads a real gh slot."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import manifest_identity as mi  # noqa: E402
import manifest_overlay as mo  # noqa: E402
import manifest_slots as ms  # noqa: E402
from manifest_protocol import ManifestError  # noqa: E402
from test_manifest_overlay import make_repo  # noqa: E402

PROJECTS = """\
[[project]]
name = "one"
prefix = "A"
repo = "lanes/one"
account = "lanes"
path = "/nonexistent/one"

[[project]]
name = "two"
prefix = "B"
repo = "other/two"
account = "plain"
path = "/nonexistent/two"
"""
LANE_SLOTS = "\n[accounts.lanes]\nlane_slots = true\n"
PLAIN = "\n[accounts.plain]\nlane_slots = false\n"


class Case(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.slots = self.root / "slots"
        self.manifest = self.root / "projects.toml"
        self.write(PROJECTS + LANE_SLOTS + PLAIN)
        env = mock.patch.dict(os.environ, {"GH_ACCT_HOME": str(self.slots),
                                           "FORGE_PROJECTS_MANIFEST": str(self.manifest)})
        env.start()
        self.addCleanup(env.stop)
        for name in ("LANE", "GH_TOKEN", "GITHUB_TOKEN"):
            os.environ.pop(name, None)  # restored with the rest of the dict

    def write(self, text: str) -> None:
        self.manifest.write_text(text)

    def lane_slot(self, lane: str, token: str | None = "tok") -> Path:
        slot = self.slots / f"lanes-lane{lane}"
        slot.mkdir(parents=True, exist_ok=True)
        if token:
            (slot / "hosts.yml").write_text(f"github.com:\n    oauth_token: {token}\n")
        return slot


class AccountsTable(Case):
    def test_absent_is_empty(self) -> None:
        self.write(PROJECTS)
        self.assertEqual(ms.accounts(), {})

    def test_reads_the_table(self) -> None:
        self.assertEqual(ms.accounts(), {"lanes": {"lane_slots": True}, "plain": {"lane_slots": False}})

    def test_a_table_without_lane_slots_refuses(self) -> None:
        """Sticky-wicket verdict: an empty table, or one whose key was deleted, never
        silently means false."""
        for text in (PROJECTS + "\n[accounts.lanes]\n" + PLAIN,
                     PROJECTS + LANE_SLOTS + "\n[accounts.plain]\n"):
            with self.subTest(text=text[-40:]):
                self.write(text)
                with self.assertRaises(ManifestError) as caught:
                    ms.accounts()
                self.assertIn("must set lane_slots", str(caught.exception))
                with mock.patch.dict(os.environ, {"LANE": "2"}), self.assertRaises(ManifestError):
                    ms.selection("lanes", "2")

    def test_an_unknown_key_or_a_non_bool_refuses(self) -> None:
        for text in (PROJECTS + "\n[accounts.lanes]\nlane_slot = true\n",
                     PROJECTS + '\n[accounts.lanes]\nlane_slots = "yes"\n',
                     "accounts = 3\n" + PROJECTS,
                     PROJECTS + "\n[accounts]\nlanes = 1\n"):
            with self.subTest(text=text[-40:]):
                self.write(text)
                with self.assertRaises(ManifestError):
                    ms.accounts()

    def test_the_table_is_invisible_to_the_project_loader(self) -> None:
        self.assertEqual(sorted(mi.by_repo()), ["lanes/one", "other/two"])


class Selection(Case):
    def test_a_lane_slots_account_in_a_lane_gets_its_lane_slot_and_token(self) -> None:
        slot = self.lane_slot("2")
        self.assertEqual(ms.selection("lanes", "2"), (slot, "tok"))

    def test_a_lane_slot_without_a_token_is_empty_not_none(self) -> None:
        slot = self.lane_slot("1", token=None)
        self.assertEqual(ms.selection("lanes", "1"), (slot, ""))

    def test_outside_a_lane_or_without_the_flag_is_the_account_slot(self) -> None:
        for account, lane in (("lanes", None), ("lanes", ""), ("lanes", "4"), ("plain", "2")):
            with self.subTest(account=account, lane=lane):
                self.assertEqual(ms.selection(account, lane), (self.slots / account, None))

    def test_a_deleted_table_with_no_lane_slot_on_disk_refuses_in_a_lane(self) -> None:
        """Preclose round 2 input: no `<acct>-lane<N>` dir exists, and the table is gone."""
        self.write(PROJECTS + PLAIN)
        self.assertFalse(self.slots.exists())
        with mock.patch.dict(os.environ, {"LANE": "2", "GH_TOKEN": "inherited"}):
            with self.assertRaises(ManifestError) as caught:
                ms.selection("lanes", "2")
            self.assertIn("has no [accounts.lanes] table", str(caught.exception))
            with self.assertRaises(ManifestError):
                mi.slot_env("lanes/one")
            self.assertIsNone(mi.slot_env_or_none("lanes/one"))
            mi._VERIFIED.clear()
            with mock.patch.object(mi, "_probe_login", return_value="lanes") as probe, \
                    self.assertRaises(ManifestError):
                mi.apply_project_identity("lanes/one")
            probe.assert_not_called()
        self.assertEqual(ms.selection("lanes", None), (self.slots / "lanes", None))

    def test_a_misspelled_table_refuses_in_a_lane(self) -> None:
        self.lane_slot("2")
        self.write(PROJECTS + PLAIN + "\n[accounts.lanez]\nlane_slots = true\n")
        with self.assertRaises(ManifestError) as caught:
            ms.selection("lanes", "2")
        self.assertIn("has no [accounts.lanes] table", str(caught.exception))
        self.assertEqual(ms.selection("lanes", None), (self.slots / "lanes", None))

    def test_a_recased_table_still_selects_the_lane_slot(self) -> None:
        slot = self.lane_slot("2")
        self.write(PROJECTS + PLAIN + "\n[accounts.LaNeS]\nlane_slots = true\n")
        self.assertEqual(ms.selection("lanes", "2"), (slot, "tok"))

    def test_the_table_lookup_ignores_case_and_refuses_a_case_duplicate(self) -> None:
        slot = self.lane_slot("2")
        self.write(PROJECTS + PLAIN + "\n[accounts.LANES]\nlane_slots = true\n")
        self.assertEqual(ms.selection("lanes", "2"), (slot, "tok"))
        self.write(PROJECTS + LANE_SLOTS + "\n[accounts.Lanes]\nlane_slots = false\n")
        with self.assertRaises(ManifestError):
            ms.accounts()

    def test_slot_env_follows_the_table(self) -> None:
        slot = self.lane_slot("2")
        with mock.patch.dict(os.environ, {"LANE": "2", "GH_TOKEN": "inherited"}):
            self.assertEqual((mi.slot_env("lanes/one")["GH_CONFIG_DIR"],
                              mi.slot_env("lanes/one")["GH_TOKEN"]), (str(slot), "tok"))
            env = mi.slot_env("other/two")
        self.assertEqual(env["GH_CONFIG_DIR"], str(self.slots / "plain"))
        self.assertNotIn("GH_TOKEN", env)

    def test_slot_env_fails_closed_on_a_token_less_lane_slot(self) -> None:
        self.lane_slot("3", token=None)
        with mock.patch.dict(os.environ, {"LANE": "3"}):
            self.assertEqual(mi.slot_env("lanes/one")["GH_TOKEN"], mi.LANE_SLOT_MISSING_TOKEN)

    def test_apply_project_identity_follows_the_table(self) -> None:
        mi._VERIFIED.clear()
        slot = self.lane_slot("1")
        with mock.patch.dict(os.environ, {"LANE": "1"}), \
                mock.patch.object(mi, "_probe_login", return_value="lanes"):
            self.assertEqual(mi.apply_project_identity("lanes/one"), "lanes")
            self.assertEqual((os.environ["GH_CONFIG_DIR"], os.environ["GH_TOKEN"]), (str(slot), "tok"))
            (slot / "hosts.yml").unlink()
            with self.assertRaises(SystemExit) as caught:
                mi.apply_project_identity("lanes/one")
        self.assertIn("harmonic-forge#1470", str(caught.exception))


class SlotRoot(Case):
    """Pitch-inspection revision 3: Python refuses a relative slot root too."""

    def test_a_relative_slot_root_refuses(self) -> None:
        for home in ("slots", "./slots"):
            with self.subTest(home=home), mock.patch.dict(os.environ, {"GH_ACCT_HOME": home}):
                with self.assertRaises(ManifestError):
                    mi.slot_dir("plain")
                self.assertIsNone(mi.slot_env_or_none("other/two"))

    def test_an_absolute_or_tilde_slot_root_resolves(self) -> None:
        with mock.patch.dict(os.environ, {"GH_ACCT_HOME": "~/s"}):
            self.assertEqual(mi.slot_dir("plain"), Path.home() / "s" / "plain")


class LaunchIdentity(Case):
    def repo(self, key: str, overlay: bool = True) -> Path:
        owner, name = key.split("/")
        text = (f'[project]\nname = "{name}"\nprefix = "Z"\nrepo = "{key}"\naccount = "{owner}"\n'
                if overlay else None)
        return make_repo(self.root / f"{owner}-{name}", f"https://github.com/{key}.git", text)

    def test_a_lane_slots_account_in_a_lane(self) -> None:
        slot = self.lane_slot("2")
        self.assertEqual(ms.launch_identity("https://github.com/lanes/one.git", self.root, "2"),
                         ("lane", "lanes", str(slot), str(slot / "hosts.yml")))

    def test_any_other_projects_toml_account_is_a_strict_slot(self) -> None:
        for url, lane, account in (("https://github.com/lanes/one.git", None, "lanes"),
                                   ("git@github.com:other/two.git", "2", "plain")):
            with self.subTest(url=url):
                self.assertEqual(ms.launch_identity(url, self.root, lane),
                                 ("slot", account, str(self.slots / account), ""))

    def test_a_registered_repo_is_never_ambient_even_with_an_overlay(self) -> None:
        repo = self.repo("other/two", overlay=False)
        self.assertEqual(ms.launch_identity("https://github.com/other/two.git", repo, "1")[0], "slot")

    def test_an_overlay_only_undeclared_account_is_ambient(self) -> None:
        repo = self.repo("acme/app")
        self.assertEqual(ms.launch_identity("https://github.com/acme/app.git", repo, "1"),
                         ("ambient", "acme", "", ""))

    def test_an_overlay_only_repo_under_a_declared_account_refuses(self) -> None:
        """Revision 1: `lanes` is a project's account, `extra` only an [accounts] key."""
        self.write(PROJECTS + LANE_SLOTS + PLAIN + "\n[accounts.extra]\nlane_slots = false\n")
        for key in ("lanes/new", "extra/new"):
            with self.subTest(key=key):
                repo = self.repo(key)
                with self.assertRaises(ManifestError) as caught:
                    ms.launch_identity(f"https://github.com/{key}.git", repo, "1")
                self.assertIn("declared in projects.toml", str(caught.exception))

    def test_an_overlay_only_repo_under_a_registered_owner_refuses(self) -> None:
        """Preclose round 1, survivor 1: `other` owns other/two, which runs as `plain`."""
        repo = self.repo("other/new")
        with self.assertRaises(ManifestError) as caught:
            ms.launch_identity("https://github.com/other/new.git", repo, "2")
        self.assertIn("declared in projects.toml", str(caught.exception))
        self.assertIn("other", ms.declared())

    def test_an_unregistered_origin_refuses(self) -> None:
        repo = self.repo("acme/app", overlay=False)
        with self.assertRaises(ManifestError):
            ms.launch_identity("https://github.com/acme/app.git", repo, "1")

    def test_main_prints_one_field_per_line(self) -> None:
        repo = self.repo("acme/app")
        with mock.patch("builtins.print") as printed:
            self.assertEqual(ms.main(["launch", "https://github.com/acme/app.git", str(repo), ""]), 0)
        printed.assert_called_once_with("ambient\nacme\n\n")


class OverlayNeverReachesASlotMode(Case):
    """TC5 (R1): the overlay can carry neither an [accounts] table nor a slot key."""

    def test_an_overlay_accounts_table_is_refused(self) -> None:
        text = ('[project]\nname = "app"\nprefix = "Z"\nrepo = "acme/app"\naccount = "acme"\n'
                "\n[accounts.acme]\nlane_slots = true\n")
        repo = make_repo(self.root / "app", "https://github.com/acme/app.git", text)
        with self.assertRaises(ManifestError) as caught:
            mo.project_for_checkout(repo, self.manifest)
        self.assertIn("unknown top-level key(s): accounts", str(caught.exception))
        with self.assertRaises(ManifestError):
            ms.launch_identity("https://github.com/acme/app.git", repo, "1")

    def test_an_overlay_project_slot_key_is_refused(self) -> None:
        text = ('[project]\nname = "app"\nprefix = "Z"\nrepo = "acme/app"\naccount = "acme"\n'
                "lane_slots = true\n")
        repo = make_repo(self.root / "app", "https://github.com/acme/app.git", text)
        with self.assertRaises(ManifestError) as caught:
            mo.project_for_checkout(repo, self.manifest)
        self.assertIn("unknown key(s): lane_slots", str(caught.exception))

    def test_this_module_never_names_the_overlay_resolver(self) -> None:
        source = (HERE / "manifest_slots.py").read_text()
        for name in ("manifest_overlay", "path_project", "project_for_checkout"):
            self.assertNotIn(name, source)


if __name__ == "__main__":
    unittest.main()
