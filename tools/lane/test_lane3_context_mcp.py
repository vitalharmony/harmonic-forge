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
        for value in ("1161", "H0", "H11;gh", "F1/path", 1161):
            with self.subTest(value=value):
                with self.assertRaisesRegex(RuntimeError, "issue must"):
                    SERVER._context(value)

    def test_prefix_routes_to_the_registered_project_not_session_cwd(self) -> None:
        target = Path("/registered/forge-lane3")
        with patch.object(SERVER, "_target_worktree", return_value=target):
            repo, number, selected = SERVER._issue_target("F326")
        self.assertEqual((repo, number, selected), ("vitalharmony/harmonic-forge", "326", target))

    def test_remote_must_be_the_canonical_github_host_and_repo(self) -> None:
        with patch.object(SERVER, "_run", return_value="https://evil.example/vitalharmony/hrse.git\n"):
            with self.assertRaisesRegex(RuntimeError, "canonical"):
                SERVER._remote_repo(Path("/registered/hrse-lane3"))
        with patch.object(SERVER, "_run", return_value="git@github.com:vitalharmony/hrse.git\n"):
            self.assertEqual(SERVER._remote_repo(Path("/registered/hrse-lane3")), "vitalharmony/hrse")

    def test_returns_only_fixed_filtered_context_and_diff_operations(self) -> None:
        seen: list[tuple[str, ...]] = []

        def run(*args: str, cwd=None) -> str:
            seen.append(args)
            if args[:2] == ("git", "rev-parse"):
                return "abc123\n"
            if args[:2] == ("git", "diff"):
                return "diff --git a/x b/x\n"
            if args[0] == "python3":
                return "# issue body and Lane 1 only\n"
            raise AssertionError(args)

        with patch.object(SERVER, "_target_worktree", return_value=Path("/registered/hrse-lane3")), \
             patch.object(SERVER, "_run", side_effect=run):
            context = SERVER._context("H1161")
        self.assertIn("issue: H1161", context)
        self.assertIn("# issue body and Lane 1 only", context)
        self.assertIn("diff --git", context)
        self.assertIn(("git", "diff", "origin/main...HEAD"), seen)
        fetch = next(call for call in seen if call[0] == "python3")
        self.assertEqual(fetch[-4:], ("--repo", "vitalharmony/hrse", "--issue", "1161"))

    def test_server_advertises_only_the_two_bounded_lane3_tools(self) -> None:
        proc = subprocess.run(
            ["python3", str(SERVER_PATH)], input=json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n",
            capture_output=True, text=True, check=True,
        )
        reply = json.loads(proc.stdout)
        tools = reply["result"]["tools"]
        self.assertEqual({tool["name"] for tool in tools}, {"fetch_context", "fetch_comment", "post_gate_report", "read_file", "list_files", "search_text", "read_directive"})
        self.assertTrue(tools[0]["inputSchema"]["additionalProperties"] is False)

    def test_extension_and_policy_allow_only_the_bounded_mcp_tools(self) -> None:
        manifest = json.loads(MANIFEST_PATH.read_text())
        servers = manifest["mcpServers"]
        self.assertEqual(set(servers), {"lane3-hrse", "lane3-forge"})
        self.assertEqual(set(servers["lane3-hrse"]["includeTools"]), {"fetch_context", "fetch_comment", "post_gate_report", "read_file", "list_files", "search_text", "read_directive"})

        policy = POLICY_PATH.read_text()
        self.assertIn('mcpName = "lane3-hrse"', policy)
        self.assertIn('mcpName = "lane3-forge"', policy)
        self.assertIn('toolName = "fetch_context"', policy)
        self.assertIn('toolName = "fetch_comment"', policy)
        self.assertIn('toolName = "post_gate_report"', policy)
        self.assertIn('mcpName = "*"', policy)
        self.assertIn('decision = "deny"', policy)

    def test_named_comment_must_belong_to_the_requested_issue(self) -> None:
        response = json.dumps({"issue_url": "https://api.github.com/repos/vitalharmony/harmonic-forge/issues/326", "body": "14-point spec"})
        with patch.object(SERVER, "_target_worktree", return_value=Path("/registered/forge-lane3")), \
             patch.object(SERVER, "_run", return_value=response) as run:
            result = SERVER._fetch_comment("F326", 123)
        self.assertIn("14-point spec", result)
        self.assertEqual(run.call_args.args[:3], ("gh", "api", "repos/vitalharmony/harmonic-forge/issues/comments/123"))

    def test_report_uses_fixed_self_checking_poster_for_matching_issue(self) -> None:
        with patch.object(SERVER, "_target_worktree", return_value=Path("/registered/hrse-lane3")), \
             patch.object(SERVER, "_run", return_value="[POST-COMMENT] Posted: https://github.com/vitalharmony/hrse/issues/1161#issuecomment-1\n[POST-COMMENT] Self-check passed: posted content matches source exactly.\n") as run:
            result = SERVER._post_gate_report("H1161", "gate_report", "PASS")
        self.assertIn("issuecomment-1", result)
        call = run.call_args.args
        self.assertEqual(call[:2], ("python3", str(SERVER_PATH.parents[3] / "tools" / "gh" / "post_comment.py")))
        self.assertEqual(call[2:6], ("--repo", "vitalharmony/hrse", "--issue", "1161"))
        self.assertIn("<!-- lane3 kind=gate_report -->", call[-1])


if __name__ == "__main__":
    unittest.main()
