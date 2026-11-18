#!/usr/bin/env python3
"""harmonic-forge#1396 preclose pass 2 (test-honesty): every board caller sends
its board command through projects_token.projects_env, so in a lane a
vitalharmony board call carries the projects slot's token. Each test removes
the routing in its own way, so a caller switched back to a plain run fails."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gh_issue  # noqa: E402
import item_list_cache  # noqa: E402
import repo_hygiene  # noqa: E402

PROJECTS = "projtok-routing"


class _LaneEnv(unittest.TestCase):
    """A lane-1 environment whose projects slot holds PROJECTS."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        (home / "vitalharmony-projects").mkdir()
        (home / "vitalharmony-projects" / "token").write_text(PROJECTS)
        env = mock.patch.dict(os.environ, {"LANE": "1", "GH_ACCT_HOME": str(home),
                                           "GH_CONFIG_DIR": str(home / "vitalharmony-lane1"),
                                           "GH_TOKEN": "lanetok"})
        env.start()
        self.addCleanup(env.stop)
        self.envs: list[dict | None] = []

    def capture(self, stdout: str = "{}"):
        """A subprocess.run stand-in that records the env each call carried."""
        def run(cmd, *args, **kwargs):
            self.envs.append(kwargs.get("env"))
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
        return run

    def assert_projects_token(self) -> None:
        self.assertTrue(self.envs, "no subprocess call was made")
        for env in self.envs:
            self.assertIsNotNone(env, "a board call inherited the session env")
            self.assertEqual(env.get("GH_TOKEN"), PROJECTS)


class GhIssueBoardCallers(_LaneEnv):
    def test_run_board_carries_the_projects_token(self):
        with mock.patch.object(gh_issue.subprocess, "run", side_effect=self.capture()):
            gh_issue._run_board(["gh", "project", "view", "1", "--owner", "vitalharmony"], check=False)
        self.assert_projects_token()

    def test_every_gh_issue_board_call_goes_through_run_board(self):
        board_cmds: list[list[str]] = []
        plain_cmds: list[list[str]] = []

        def board(cmd, check=True):
            board_cmds.append(cmd)
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="stub")

        def plain(cmd, check=True, env=None):
            plain_cmds.append(cmd)
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="stub")

        with mock.patch.object(gh_issue, "_run_board", side_effect=board), \
                mock.patch.object(gh_issue, "_run", side_effect=plain), \
                mock.patch("sys.stderr"), mock.patch("sys.stdout"):
            gh_issue._fetch_project_context("vitalharmony", "1")
            gh_issue._set_single_select("IT_1", "PVT_1", [{"name": "Tier", "id": "F", "options": [
                {"name": "fast", "id": "O"}]}], "Tier", "fast")
            gh_issue._find_existing_item("https://github.com/vitalharmony/hrse/issues/1",
                                         "vitalharmony", "1")
            gh_issue.add_to_board("https://github.com/vitalharmony/hrse/issues/1", "vitalharmony", "1",
                                  None)
        board_verbs = {tuple(c[1:3]) for c in board_cmds}
        self.assertIn(("project", "view"), board_verbs)
        self.assertIn(("project", "item-edit"), board_verbs)
        self.assertIn(("api", "graphql"), board_verbs)
        self.assertIn(("project", "item-add"), board_verbs)
        self.assertFalse([c for c in plain_cmds if c[1] == "project" or "graphql" in c],
                         f"board commands bypassed _run_board: {plain_cmds}")


class ItemListCacheDefaultRuns(_LaneEnv):
    def test_the_default_field_read_carries_the_projects_token_for_the_board_owner(self):
        with mock.patch("subprocess.run", side_effect=self.capture('{"data": {}}')):
            try:
                item_list_cache._fetch_issue_field_live("someone-else/repo", 1, "1",
                                                        board_owner="vitalharmony")
            except Exception:  # noqa: BLE001 - the parse of the stub answer is not under test
                pass
        self.assert_projects_token()

    def test_the_default_board_currency_read_carries_the_projects_token(self):
        with mock.patch("subprocess.run", side_effect=self.capture('{"data": {}}')):
            item_list_cache._board_currency("vitalharmony", "1")
        self.assert_projects_token()


class RepoHygieneBoardQuery(_LaneEnv):
    def test_the_board_query_carries_the_projects_token(self):
        with mock.patch.object(repo_hygiene.subprocess, "run", side_effect=self.capture('{"data": {}}')):
            repo_hygiene._run(["gh", "api", "graphql", "-f", "query=q", "-F", "owner=vitalharmony"],
                              board=True)
        self.assert_projects_token()


if __name__ == "__main__":
    unittest.main()
