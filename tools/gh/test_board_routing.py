#!/usr/bin/env python3
"""harmonic-forge#1396: every board caller sends its board command through
projects_token.projects_env, so in a lane a vitalharmony board call carries the
projects slot's token and another account's board call does not.

Every test here patches only `subprocess.run` and drives a real caller, so a
call site switched back to a plain run (post-verdict survivor: the first version
of these tests stubbed the routing helpers and missed four sites) fails here."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import board_fields  # noqa: E402
import gh_issue  # noqa: E402
import item_list_cache  # noqa: E402
import repo_hygiene  # noqa: E402

PROJECTS = "projtok-routing"
LANE_TOKEN = "lanetok"


class _Lane(unittest.TestCase):
    """A lane-1 session whose projects slot holds PROJECTS; every subprocess
    call is captured with the env it carried."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        (self.home / "vitalharmony-projects").mkdir()
        (self.home / "vitalharmony-projects" / "token").write_text(PROJECTS)
        (self.home / "vitalharmony-lane1").mkdir()  # harmonic-forge#1463: a granted slot must exist
        (self.home / "vitalharmony-lane1" / "token").write_text(LANE_TOKEN)  # harmonic-forge#1466
        env = mock.patch.dict(os.environ, {"LANE": "1", "GH_ACCT_HOME": str(self.home),
                                           "GH_CONFIG_DIR": str(self.home / "vitalharmony-lane1"),
                                           "GH_TOKEN": LANE_TOKEN})
        env.start()
        self.addCleanup(env.stop)
        self.calls: list[tuple[list[str], dict | None]] = []
        self.answer = "{}"

    def fake_run(self, cmd, *args, **kwargs):
        self.calls.append((list(cmd), kwargs.get("env")))
        return subprocess.CompletedProcess(cmd, 0, stdout=self.answer, stderr="")

    def patch_subprocess(self):
        return mock.patch("subprocess.run", side_effect=self.fake_run)

    def tokens(self) -> list[str | None]:
        return [(env or os.environ).get("GH_TOKEN") for _cmd, env in self.calls]

    def assert_all_projects(self) -> None:
        self.assertTrue(self.calls, "no board call reached subprocess")
        self.assertEqual(set(self.tokens()), {PROJECTS}, self.calls)


class GhIssueCallers(_Lane):
    def test_project_view_and_field_list(self):
        self.answer = json.dumps({"id": "PVT_1", "fields": []})
        with self.patch_subprocess(), mock.patch("sys.stderr"):
            gh_issue._fetch_project_context("vitalharmony", "1")
        self.assertGreaterEqual(len(self.calls), 2)
        self.assert_all_projects()

    def test_item_edit(self):
        with self.patch_subprocess(), mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            gh_issue._set_single_select("IT_1", "PVT_1", [{"name": "Tier", "id": "F", "options": [
                {"name": "fast", "id": "O"}]}], "Tier", "fast")
        self.assert_all_projects()

    def test_existing_item_lookup_keys_on_the_board_owner(self):
        # Post-verdict survivor 1: the issue's repo owner is another account,
        # the board is vitalharmony's.
        self.answer = json.dumps({"data": {}})
        with self.patch_subprocess():
            gh_issue._find_existing_item("https://github.com/otherorg/r/issues/3", "vitalharmony", "1")
        self.assert_all_projects()

    def test_item_add(self):
        self.answer = json.dumps({"id": "IT_1"})
        with self.patch_subprocess(), mock.patch.object(gh_issue, "_fetch_project_context",
                                                        return_value=None), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            gh_issue.add_to_board("https://github.com/vitalharmony/hrse/issues/1", "vitalharmony", "1",
                                  None)
        adds = [env for cmd, env in self.calls if cmd[1:3] == ["project", "item-add"]]
        self.assertTrue(adds)
        self.assertEqual({(e or {}).get("GH_TOKEN") for e in adds}, {PROJECTS})

    def test_another_accounts_board_keeps_the_lane_token(self):
        self.answer = json.dumps({"data": {}})
        with self.patch_subprocess():
            gh_issue._find_existing_item("https://github.com/vitalharmony/hrse/issues/3",
                                         "harmonicarchitect", "1")
        self.assertEqual(set(self.tokens()), {LANE_TOKEN})


class BoardFieldsCallers(_Lane):
    def test_find_item_keys_on_the_board_owner(self):
        self.answer = json.dumps({"data": {"repository": {"issue": {"projectItems": {"nodes": []}}}}})
        with self.patch_subprocess():
            try:
                board_fields.find_item("otherorg/r", 1, "vitalharmony", "1")
            except Exception:  # noqa: BLE001 - the stub answer's parse is not under test
                pass
        self.assert_all_projects()

    def test_the_refetch_runner(self):
        with self.patch_subprocess():
            board_fields.board_runner("vitalharmony")(["gh", "api", "graphql", "-F", "owner=otherorg"])
        self.assert_all_projects()


class ItemListCacheCallers(_Lane):
    def test_full_board_fetch(self):
        self.answer = ""
        with self.patch_subprocess(), tempfile.TemporaryDirectory() as cache:
            try:
                item_list_cache.fetch_full_board("1", owner="vitalharmony", cache_dir=Path(cache),
                                                 force=True)
            except Exception:  # noqa: BLE001
                pass
        self.assert_all_projects()

    def test_targeted_field_read_keys_on_the_board_owner(self):
        self.answer = json.dumps({"data": {}})
        with self.patch_subprocess():
            try:
                item_list_cache._fetch_issue_field_live("otherorg/r", 1, "1", board_owner="vitalharmony")
            except Exception:  # noqa: BLE001
                pass
        self.assert_all_projects()

    def test_board_currency(self):
        self.answer = json.dumps({"data": {}})
        with self.patch_subprocess():
            item_list_cache._board_currency("vitalharmony", "1")
        self.assert_all_projects()

    def test_cross_owner_scan(self):
        self.answer = json.dumps({"data": {"repository": {"issue": {"projectItems": {"nodes": []}}}}})
        with self.patch_subprocess(), tempfile.TemporaryDirectory() as cache, \
                mock.patch.object(item_list_cache, "_scan_issue_field",
                                  side_effect=lambda *a: a[-1](["gh", "project", "item-list", "1"])):
            item_list_cache.fetch_issue_field("otherorg/r", 1, "1", ttl=0, cache_dir=Path(cache),
                                              owner="vitalharmony")
        self.assert_all_projects()


class RepoHygieneCaller(_Lane):
    def test_board_query(self):
        self.answer = json.dumps({"data": {}})
        with self.patch_subprocess():
            repo_hygiene._run_board_query("query { x }", "vitalharmony", "1", None)
        self.assert_all_projects()


if __name__ == "__main__":
    unittest.main()
