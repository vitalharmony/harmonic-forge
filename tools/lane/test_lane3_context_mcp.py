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
        target = Path("/registered/hrse-lane3")
        attested = SERVER.binding.AttestedTarget("vitalharmony/hrse", "a" * 40, target)
        with patch.object(SERVER, "_target_worktree", return_value=Path("/registered/hrse-lane3")), \
             patch.object(SERVER, "_provider", return_value=Path("/trusted/provider.py")), \
             patch.object(SERVER.binding, "filtered_context", return_value="# issue body and Lane 1 only\n") as context_fetch, \
             patch.object(SERVER.binding, "resolve_target", return_value=attested) as resolve, \
             patch.object(SERVER.binding, "diff_from_main", return_value="diff --git a/x b/x\n"):
            context = SERVER._context("H1161")
        self.assertIn("issue: H1161", context)
        self.assertIn("# issue body and Lane 1 only", context)
        self.assertIn("diff --git", context)
        self.assertIn(f"target_sha: {'a' * 40}", context)
        context_fetch.assert_called_once_with(
            Path("/trusted/provider.py"), "vitalharmony/hrse", "1161", target,
        )
        resolve.assert_called_once_with(
            Path("/trusted/provider.py"), "vitalharmony/hrse", "1161", target,
        )

    def test_pre_ae_context_succeeds_without_target_or_diff_exposure(self) -> None:
        target = Path("/registered/forge-lane3")
        with patch.object(SERVER, "_target_worktree", return_value=target), \
             patch.object(SERVER, "_provider", return_value=Path("/trusted/provider.py")), \
             patch.object(SERVER.binding, "filtered_context", return_value="# safe context\n"), \
             patch.object(SERVER.binding, "resolve_target", side_effect=SERVER.binding.NoAttestedTarget("no current AE")), \
             patch.object(SERVER.binding, "diff_from_main") as diff:
            context = SERVER._context("F326")
        self.assertIn("# safe context", context)
        self.assertNotIn("target_sha:", context)
        self.assertNotIn("Target diff", context)
        diff.assert_not_called()

    def test_post_ae_binding_failures_are_not_downgraded_to_pre_ae_context(self) -> None:
        target = Path("/registered/forge-lane3")
        with patch.object(SERVER, "_target_worktree", return_value=target), \
             patch.object(SERVER, "_provider", return_value=Path("/trusted/provider.py")), \
             patch.object(SERVER.binding, "filtered_context", return_value="# safe context\n"), \
             patch.object(SERVER.binding, "resolve_target", side_effect=RuntimeError("invalid metadata")):
            with self.assertRaisesRegex(RuntimeError, "invalid metadata"):
                SERVER._context("F326")

    def test_target_backed_operations_still_fail_closed_without_current_ae(self) -> None:
        target = Path("/registered/forge-lane3")
        with patch.object(SERVER, "_target_worktree", return_value=target), \
             patch.object(SERVER, "_provider", return_value=Path("/trusted/provider.py")), \
             patch.object(SERVER.binding, "resolve_target", side_effect=SERVER.binding.NoAttestedTarget("no current AE")):
            with self.assertRaisesRegex(SERVER.binding.NoAttestedTarget, "no current AE"):
                SERVER._attested_target("F326")

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
        file_tools = {tool["name"]: tool for tool in tools if tool["name"] in SERVER.FILE_TOOLS}
        self.assertTrue(all("issue" in tool["inputSchema"]["required"] for tool in file_tools.values()))
        self.assertTrue(all("repository" not in tool["inputSchema"]["properties"] for tool in file_tools.values()))
        self.assertTrue(all("sha" not in tool["inputSchema"]["properties"] for tool in file_tools.values()))

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
        with patch.object(SERVER, "_target_worktree", return_value=Path("/registered/forge-lane3")), \
             patch.object(SERVER, "_provider", return_value=Path("/trusted/provider.py")), \
             patch.object(SERVER.binding, "named_comment", return_value="14-point spec") as fetch:
            result = SERVER._fetch_comment("F326", 123)
        self.assertIn("14-point spec", result)
        fetch.assert_called_once_with(
            Path("/trusted/provider.py"), "vitalharmony/harmonic-forge", "326", 123,
            Path("/registered/forge-lane3"),
        )

    def test_adapter_has_no_raw_github_transport(self) -> None:
        source = SERVER_PATH.read_text()
        self.assertNotIn('"gh", "api"', source)
        self.assertNotIn("issues/comments", source)

    def test_target_is_recomputed_without_cross_call_state(self) -> None:
        target = Path("/registered/forge-lane3")
        attested = SERVER.binding.AttestedTarget(
            "vitalharmony/harmonic-forge", "a" * 40, target,
        )
        with patch.object(SERVER, "_target_worktree", return_value=target), \
             patch.object(SERVER, "_provider", return_value=Path("/trusted/provider.py")), \
             patch.object(SERVER.binding, "resolve_target", return_value=attested) as resolve:
            SERVER._attested_target("F326")
            SERVER._attested_target("F326")
        self.assertEqual(resolve.call_count, 2)
        self.assertNotIn("tempfile", SERVER_PATH.read_text())

    def test_shared_lane_launchers_are_unchanged(self) -> None:
        root = SERVER_PATH.parents[3]
        result = subprocess.run(
            ["git", "diff", "--exit-code", "origin/main", "--", "tools/lane/lane2", "tools/lane/lane3"],
            cwd=root, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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
