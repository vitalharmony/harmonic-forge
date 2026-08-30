"""Unit checks for H1414's bounded Gemini Lane 3 MCP tool."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SERVER_PATH = Path(__file__).parents[1] / "gemini" / "lane3-context" / "lane3_context_mcp.py"
MANIFEST_PATH = SERVER_PATH.parent / "gemini-extension.json"
POLICY_PATH = Path(__file__).parent / "policies" / "gemini-lane3.toml"
SPEC = importlib.util.spec_from_file_location("lane3_context_mcp", SERVER_PATH)
assert SPEC and SPEC.loader
SERVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVER)


class Lane3ContextMcpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(os.environ, {"LANE": "3", "LANE_AGENT": "gemini"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_only_a_prefixed_numeric_issue_is_accepted(self) -> None:
        with patch.object(SERVER, "_is_canonical_lane3_worktree", return_value=True):
            for value in ("1161", "H0", "H11;gh", "F1/path", 1161):
                with self.subTest(value=value):
                    with self.assertRaisesRegex(RuntimeError, "issue must"):
                        SERVER._context(value)

    def test_prefix_must_match_the_current_worktree_remote(self) -> None:
        with patch.object(SERVER, "_is_canonical_lane3_worktree", return_value=True), \
             patch.object(SERVER, "_remote_repo", return_value="vitalharmony/harmonic-forge"):
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                SERVER._context("H1161")

    def test_remote_must_be_the_canonical_github_host_and_repo(self) -> None:
        with patch.object(SERVER, "_run", return_value="https://evil.example/vitalharmony/hrse.git\n"):
            with self.assertRaisesRegex(RuntimeError, "canonical"):
                SERVER._remote_repo()
        with patch.object(SERVER, "_run", return_value="git@github.com:vitalharmony/hrse.git\n"):
            self.assertEqual(SERVER._remote_repo(), "vitalharmony/hrse")

    def test_context_requires_the_launcher_selected_lane3_worktree(self) -> None:
        with patch.object(SERVER, "_is_canonical_lane3_worktree", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "canonical Lane 3"):
                SERVER._context("H1161")

    def test_worktree_binding_requires_main_and_lane3_in_one_registered_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            main, target = root / "forge", root / "forge-lane3"
            main.mkdir()
            target.mkdir()
            (main / ".git").write_text("gitdir: elsewhere\n")
            with patch.object(SERVER.Path, "cwd", return_value=target), \
                 patch.object(SERVER, "_run", return_value=f"worktree {target}\n"):
                self.assertFalse(SERVER._is_canonical_lane3_worktree())

    def test_returns_only_fixed_filtered_context_and_diff_operations(self) -> None:
        seen: list[tuple[str, ...]] = []

        def run(*args: str) -> str:
            seen.append(args)
            if args[:2] == ("git", "rev-parse"):
                return "abc123\n"
            if args[:2] == ("git", "diff"):
                return "diff --git a/x b/x\n"
            if args[0] == "python3":
                return "# issue body and Lane 1 only\n"
            raise AssertionError(args)

        with patch.object(SERVER, "_is_canonical_lane3_worktree", return_value=True), \
             patch.object(SERVER, "_remote_repo", return_value="vitalharmony/hrse"), \
             patch.object(SERVER, "_run", side_effect=run):
            context = SERVER._context("H1161")
        self.assertIn("issue: H1161", context)
        self.assertIn("# issue body and Lane 1 only", context)
        self.assertIn("diff --git", context)
        self.assertIn(("git", "diff", "origin/main...HEAD"), seen)
        fetch = next(call for call in seen if call[0] == "python3")
        self.assertEqual(fetch[-4:], ("--repo", "vitalharmony/hrse", "--issue", "1161"))

    def test_server_advertises_exactly_one_bounded_tool(self) -> None:
        proc = subprocess.run(
            ["python3", str(SERVER_PATH)], input=json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n",
            capture_output=True, text=True, check=True,
        )
        reply = json.loads(proc.stdout)
        tools = reply["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], ["fetch_context"])
        self.assertTrue(tools[0]["inputSchema"]["additionalProperties"] is False)

    def test_extension_and_policy_allow_only_the_bounded_mcp_tool(self) -> None:
        manifest = json.loads(MANIFEST_PATH.read_text())
        server = manifest["mcpServers"]["lane3-context"]
        self.assertEqual(server["includeTools"], ["fetch_context"])

        policy = POLICY_PATH.read_text()
        self.assertIn('mcpName = "lane3-context"', policy)
        self.assertIn('toolName = "fetch_context"', policy)
        self.assertIn('mcpName = "*"', policy)
        self.assertIn('decision = "deny"', policy)


if __name__ == "__main__":
    unittest.main()
