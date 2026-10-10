#!/usr/bin/env python3
"""Tests for `require_html_gmail_draft.py` (harmonic-forge#950 AC4). Hermetic:
each case runs the hook as a subprocess on a crafted payload; nothing calls Gmail."""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parent / "require_html_gmail_draft.py"


def run(stdin: str) -> dict | None:
    result = subprocess.run([sys.executable, str(HOOK)], input=stdin,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout) if result.stdout.strip() else None


def payload(tool: str, **tool_input) -> str:
    return json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool,
                       "tool_input": tool_input})


def denied(out: dict | None) -> bool:
    return bool(out) and out["hookSpecificOutput"]["permissionDecision"] == "deny"


class RequireHtmlGmailDraft(unittest.TestCase):
    def test_plain_is_denied_with_the_fix_named(self):
        out = run(payload("mcp__workspace-vh__draft_gmail_message", body_format="plain", body="x"))
        self.assertTrue(denied(out))
        self.assertIn('body_format: "html"', out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_missing_body_format_is_denied_because_the_tool_defaults_to_plain(self):
        self.assertTrue(denied(run(payload("mcp__workspace-vh__draft_gmail_message", body="x"))))

    def test_html_is_allowed(self):
        self.assertIsNone(run(payload("mcp__workspace-vh__draft_gmail_message",
                                      body_format="html", body="<p>x</p>")))

    def test_another_workspace_server_is_covered(self):
        self.assertTrue(denied(run(payload("mcp__workspace-kenekted__draft_gmail_message",
                                           body_format="plain"))))

    def test_a_non_draft_tool_is_allowed(self):
        self.assertIsNone(run(payload("mcp__workspace-vh__send_gmail_message", body_format="plain")))
        self.assertIsNone(run(payload("Bash", command="ls")))

    def test_a_malformed_payload_is_allowed(self):
        self.assertIsNone(run("not json"))
        self.assertIsNone(run(json.dumps(["a", "list"])))
        self.assertIsNone(run(json.dumps({"tool_name": "mcp__x__draft_gmail_message",
                                          "tool_input": "a string"})))
        # Falsy malformed shapes fail open too (cross-family refutation, preclose pass 1).
        for bad in ([], "", 0):
            self.assertIsNone(run(json.dumps({"tool_name": "mcp__x__draft_gmail_message",
                                              "tool_input": bad})), bad)

    def test_a_null_tool_input_is_checked_like_a_missing_body_format(self):
        self.assertTrue(denied(run(json.dumps({"tool_name": "mcp__x__draft_gmail_message",
                                               "tool_input": None}))))


if __name__ == "__main__":
    unittest.main()
