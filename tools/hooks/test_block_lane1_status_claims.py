#!/usr/bin/env python3
"""Unit tests for block_lane1_status_claims.py's cwd threading (harmonic-forge#210).

Narrowly scoped to the cwd-threading fix itself, not this file's full
surface (autoclose-keyword denial, Lane 2/3 worktree checks, etc.) —
that's separately-scoped follow-up work. Run: python3
tools/hooks/test_block_lane1_status_claims.py"""

import sys
import unittest.mock
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import block_lane1_status_claims as m


def _is_denied(result: dict) -> bool:
    return result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


class TestCwdThreading(unittest.TestCase):
    def test_decision_resolves_relative_body_file_against_passed_cwd(self):
        """The actual bug, with a real differentiating case:
        pr_body_autoclose_text() resolves a *relative* --body-file path
        against `cwd` (`path = cwd / path` when not absolute). A relative
        path resolves to a real file (containing an autoclose keyword)
        when cwd is the directory that actually holds it, and to nothing
        when cwd is elsewhere — this produces a genuinely different
        decision() outcome depending on which cwd value is used, proving
        the payload's cwd (not Path.cwd()) is what's actually consulted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            body_dir = Path(tmpdir)
            (body_dir / "pr-body.txt").write_text("Closes #123\n")

            # Relative path, cwd = the directory that actually holds it -> denied.
            result_correct_cwd = m.decision(
                "gh pr create --title x --body-file pr-body.txt", body_dir,
            )
            self.assertTrue(_is_denied(result_correct_cwd))

            # Same relative path, cwd = somewhere else entirely -> file not
            # found there, OSError caught, no match -> allowed.
            with tempfile.TemporaryDirectory() as elsewhere:
                result_wrong_cwd = m.decision(
                    "gh pr create --title x --body-file pr-body.txt", Path(elsewhere),
                )
                self.assertFalse(_is_denied(result_wrong_cwd))

    def test_gate_checkout_denial_unaffected_by_cwd(self):
        """gate-checkout's LANE-3-only denial is LANE-based, not
        cwd-based — confirms passing a scratch cwd doesn't accidentally
        suppress or alter this unrelated check."""
        import os
        os.environ.pop("LANE", None)
        with tempfile.TemporaryDirectory() as scratch:
            result = m.decision("mise run gate-checkout main", Path(scratch))
        self.assertTrue(_is_denied(result))


class TestExistingRegressions(unittest.TestCase):
    """#167-era regressions, re-confirmed with cwd explicitly passed."""

    def test_ordinary_command_passes_through(self):
        result = m.decision("ls -la", Path.cwd())
        self.assertEqual(result, {})

    def test_gate_checkout_denied_without_lane3(self):
        import os
        os.environ.pop("LANE", None)
        result = m.decision("mise run gate-checkout main", Path.cwd())
        self.assertTrue(_is_denied(result))

    def test_autoclose_keyword_in_body_file_denied(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("Closes #123\n")
            body_path = f.name
        try:
            result = m.decision(f"gh pr create --title x --body-file {body_path}", Path.cwd())
            self.assertTrue(_is_denied(result))
        finally:
            Path(body_path).unlink()


class TestBulkCommentReadDenial(unittest.TestCase):
    """harmonic-forge#260: a fifth Lane 3 contamination incident on
    hrse#793 came through a Claude Code Lane 3 session -- #258 added the
    equivalent check to the Codex-side gate_codex_tool.py hook only, and
    this canonical file (what a Claude Lane 3 session's .claude/settings.json
    actually wires) had no check at all, so `gh issue view --comments`
    sailed through unblocked. These are the literal commands from the
    real incidents."""

    def test_gh_issue_view_comments_denied_for_lane3(self):
        import os
        with unittest.mock.patch.dict(os.environ, {"LANE": "3"}):
            result = m.decision("gh issue view 793 --repo vitalharmony/hrse --comments", Path.cwd())
        self.assertTrue(_is_denied(result))

    def test_gh_api_bulk_comments_paginate_denied_for_lane3(self):
        import os
        with unittest.mock.patch.dict(os.environ, {"LANE": "3"}):
            result = m.decision("gh api repos/vitalharmony/hrse/issues/793/comments --paginate", Path.cwd())
        self.assertTrue(_is_denied(result))

    def test_fetch_lane1_context_script_allowed_for_lane3(self):
        import os
        with unittest.mock.patch.dict(os.environ, {"LANE": "3"}):
            result = m.decision(
                "python3 ~/harmonic-forge/tools/gh/fetch_lane1_context.py --repo vitalharmony/hrse --issue 793",
                Path.cwd(),
            )
        self.assertEqual(result, {})

    def test_single_comment_by_id_allowed_for_lane3(self):
        import os
        with unittest.mock.patch.dict(os.environ, {"LANE": "3"}):
            result = m.decision("gh api repos/vitalharmony/hrse/issues/comments/5273884235", Path.cwd())
        self.assertEqual(result, {})

    def test_issue_body_only_fetch_allowed_for_lane3(self):
        import os
        with unittest.mock.patch.dict(os.environ, {"LANE": "3"}):
            result = m.decision("gh api repos/vitalharmony/hrse/issues/793 --jq .body", Path.cwd())
        self.assertEqual(result, {})

    def test_gh_issue_view_comments_allowed_for_lane2(self):
        import os
        with unittest.mock.patch.dict(os.environ, {"LANE": "2"}):
            result = m.decision("gh issue view 793 --repo vitalharmony/hrse --comments", Path.cwd())
        self.assertEqual(result, {})

    def test_gh_issue_view_comments_allowed_for_lane1(self):
        import os
        with unittest.mock.patch.dict(os.environ, {"LANE": "1"}):
            result = m.decision("gh issue view 793 --repo vitalharmony/hrse --comments", Path.cwd())
        self.assertEqual(result, {})


class TestMalformedPayload(unittest.TestCase):
    def test_non_string_command_fails_closed(self):
        """This file's existing default for posting controls is fail-
        CLOSED (the opposite of mypy_cwd_trap.py's fail-open default) —
        confirmed unchanged by the cwd-threading fix."""
        result = m.decision(None, Path.cwd())
        self.assertTrue(_is_denied(result))


if __name__ == "__main__":
    unittest.main()
