"""harmonic-forge#1425: l1_post's board commands carry the projects slot's token."""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import l1_post  # noqa: E402


class BoardCommandTokenTests(unittest.TestCase):
    def _env_of(self, *args, env=None):
        seen = {}

        def fake_run(cmd, **kw):
            seen["env"] = kw.get("env")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch("l1_post.subprocess.run", side_effect=fake_run):
            l1_post.run(*args, env=env)
        return seen["env"]

    def test_board_command_uses_projects_env(self):
        with mock.patch("projects_token.projects_env", return_value={"GH_TOKEN": "proj"}) as pe:
            env = self._env_of("gh", "project", "field-list", "3", "--owner", "vitalharmony")
        pe.assert_called_once()
        self.assertEqual(env, {"GH_TOKEN": "proj"})

    def test_other_commands_keep_the_process_env(self):
        with mock.patch("projects_token.projects_env") as pe:
            env = self._env_of("gh", "api", "repos/o/r/issues/1")
        pe.assert_not_called()
        self.assertIsNone(env)

    def test_explicit_env_is_never_replaced(self):
        with mock.patch("projects_token.projects_env") as pe:
            env = self._env_of("gh", "project", "item-list", "3", env={"X": "1"})
        pe.assert_not_called()
        self.assertEqual(env, {"X": "1"})

    def test_board_run_passes_owner_to_projects_env(self):
        seen = {}

        def fake_run(cmd, **kw):
            seen["env"] = kw.get("env")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch("projects_token.projects_env", return_value={"GH_TOKEN": "proj"}) as pe, \
                mock.patch("l1_post.subprocess.run", side_effect=fake_run):
            l1_post.board_run(["gh", "api", "graphql", "-f", "query=q"], "vitalharmony")
        self.assertEqual(pe.call_args.kwargs.get("owner"), "vitalharmony")
        self.assertEqual(seen["env"], {"GH_TOKEN": "proj"})


class ItemListCacheCallSitesTests(unittest.TestCase):
    """#1425 pass 1 survivor 2: every item_list_cache board read is handed board_run."""

    def _fake_cache(self, seen):
        class GhItemListError(Exception):
            pass

        def capture(name, result):
            def fn(*a, run=None, **kw):
                seen[name] = run
                return result
            return fn

        return type("Cache", (), {
            "GhItemListError": GhItemListError,
            "fetch_issue_tier": staticmethod(capture("tier", None)),
            "fetch_full_board": staticmethod(capture("full", [])),
            "fetch_issue_field": staticmethod(capture("field", "x")),
        })

    def _assert_board_run(self, run):
        with mock.patch("l1_post.board_run", return_value="sentinel") as br:
            self.assertEqual(run(["gh", "api", "graphql"]), "sentinel")
        br.assert_called_once_with(["gh", "api", "graphql"], "vitalharmony")

    def test_tier_and_its_full_board_fallback(self):
        seen = {}
        with mock.patch("l1_post._item_list_cache", self._fake_cache(seen)):
            # A cross-owner board falls through from the per-issue read to the full scan.
            l1_post.resolve_board_tier("other/repo", "vitalharmony", "3", 1)
        for name in ("tier", "full"):
            self._assert_board_run(seen[name])

    def test_field_reads(self):
        seen = {}
        with mock.patch("l1_post._item_list_cache", self._fake_cache(seen)):
            l1_post.read_board_fields("vitalharmony/harmonic-forge", "vitalharmony", "3", 1, ["Tier"])
        self._assert_board_run(seen["field"])

    def test_field_full_board_fallback(self):
        seen = {}
        cache = self._fake_cache(seen)
        with mock.patch("l1_post._item_list_cache", cache):
            # A cross-owner board skips the per-issue read and takes the full-board path.
            l1_post.read_board_fields("other/repo", "vitalharmony", "3", 1, ["Tier"])
        self._assert_board_run(seen["full"])


if __name__ == "__main__":
    unittest.main()
