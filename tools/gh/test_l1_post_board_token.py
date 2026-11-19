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


if __name__ == "__main__":
    unittest.main()
