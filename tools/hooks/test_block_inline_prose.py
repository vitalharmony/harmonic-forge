#!/usr/bin/env python3
"""Tests for block_inline_prose.py (harmonic-forge#266)."""

import unittest
from unittest.mock import patch

import block_inline_prose as hook


def denied(command: str) -> bool:
    return hook.decision(command).get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


class GitCommitTests(unittest.TestCase):
    def test_backtick_in_message_is_denied(self):
        """The live 2026-08-12 failure: bash ran the backticked command and
        substituted 36 lines of output into the commit message."""
        self.assertTrue(denied('git commit -m "docs: adds `mise run hygiene` to the task list"'))

    def test_dollar_paren_substitution_is_denied(self):
        self.assertTrue(denied('git commit -m "release $(date +%Y)"'))

    def test_multiline_message_is_denied(self):
        self.assertTrue(denied('git commit -m "subject line\n\nbody paragraph here"'))

    def test_ordinary_one_liner_is_allowed(self):
        """Must not make everyday commits annoying — that is how guards get
        switched off."""
        self.assertFalse(denied('git commit -m "fix: correct the off-by-one in board_sync"'))

    def test_commit_F_file_is_allowed(self):
        self.assertFalse(denied("git commit -F /tmp/msg.txt"))

    def test_heredoc_commit_is_allowed(self):
        """`-F -` with a quoted heredoc is the recommended pattern; its body is
        masked before parsing, so it must not trip the check."""
        self.assertFalse(denied("git commit -F - <<'EOF'\nsubject\n\nbody with `backticks`\nEOF"))

    def test_commit_with_flags_before_message(self):
        self.assertTrue(denied('git commit -q -m "line one\nline two"'))


class GhPrTests(unittest.TestCase):
    def test_long_inline_body_is_denied(self):
        body = "x" * 400
        self.assertTrue(denied(f'gh pr create --title t --body "{body}"'))

    def test_backtick_body_is_denied(self):
        self.assertTrue(denied('gh pr create --title t --body "see `ls` output"'))

    def test_body_file_is_allowed(self):
        self.assertFalse(denied("gh pr create --title t --body-file /tmp/body.md"))

    def test_short_plain_body_is_allowed(self):
        self.assertFalse(denied('gh pr create --title t --body "trivial docs fix"'))

    def test_pr_edit_is_covered_too(self):
        self.assertTrue(denied('gh pr edit 12 --body "see `git log` for detail"'))

    def test_unrelated_gh_commands_untouched(self):
        self.assertFalse(denied("gh pr list --json number"))
        self.assertFalse(denied("gh api repos/o/r/issues --paginate"))


class SafetyTests(unittest.TestCase):
    def test_unparseable_command_fails_open(self):
        """This guard protects prose quality, not safety. Wedging every Bash
        call over an unparseable command would be far worse than one ugly
        commit message."""
        with patch.object(hook, "command_segments", side_effect=ValueError("boom")):
            self.assertFalse(denied('git commit -m "a\nb"'))

    def test_escape_hatch(self):
        with patch.dict("os.environ", {"ALLOW_INLINE_PROSE": "1"}):
            self.assertFalse(denied('git commit -m "a\nb"'))

    def test_empty_and_non_string_commands(self):
        self.assertEqual(hook.decision(""), {})
        self.assertEqual(hook.decision(None), {})

    def test_denial_message_names_the_fix(self):
        out = hook.decision('git commit -m "a\nb"')
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("-F", reason, "a deny must state the alternative, not just refuse")


if __name__ == "__main__":
    unittest.main()
