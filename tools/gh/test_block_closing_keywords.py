#!/usr/bin/env python3
"""Tests for block_closing_keywords.py (harmonic-forge#84/#93, and the
harmonic-forge#612 live-BATCH exception).

No test file existed for this hook before #612 -- this is the first, and it
covers both the pre-existing blanket-deny behavior and the new live-BATCH
exception together, since #612 is the first change to ever touch this file.
"""
import json
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

HOOK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOK_DIR))
import block_closing_keywords as bck  # noqa: E402


def _run_hook(command: str) -> dict:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    out = []
    with mock.patch("sys.stdin.read", return_value=payload), \
         mock.patch("json.load", return_value=json.loads(payload)), \
         mock.patch("builtins.print", side_effect=lambda s: out.append(s)):
        bck.main()
    return json.loads(out[0])


def _is_denied(result: dict) -> bool:
    return result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


class NonClosingCommandsPassThrough(unittest.TestCase):
    def test_irrelevant_command_is_untouched(self):
        result = _run_hook("gh pr view 42 --repo vitalharmony/hrse")
        self.assertEqual(result, {})

    def test_non_closing_reference_is_untouched(self):
        result = _run_hook(
            'gh pr create --repo vitalharmony/hrse --title x --body "Implements #42"'
        )
        self.assertEqual(result, {})

    def test_part_of_reference_is_untouched(self):
        result = _run_hook(
            'gh pr create --repo vitalharmony/hrse --title x --body "Part of #42"'
        )
        self.assertEqual(result, {})


class ClosingKeywordDeniedWithNoLiveBatch(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(__import__("tempfile").mkdtemp()) / "batch-authorized.json"
        self.patcher = mock.patch.object(bck, "BATCH_STATE_PATH", self.tmp)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_no_state_file_denies(self):
        result = _run_hook(
            'gh pr create --repo vitalharmony/hrse --title x --body "Closes #42"'
        )
        self.assertTrue(_is_denied(result))
        self.assertIn("harmonic-forge#612", result["systemMessage"])

    def test_fixes_keyword_denies(self):
        self.tmp.write_text("{}")
        result = _run_hook(
            'gh pr create --repo vitalharmony/hrse --title x --body "Fixes #42"'
        )
        self.assertTrue(_is_denied(result))

    def test_empty_state_denies(self):
        self.tmp.write_text("{}")
        result = _run_hook(
            'gh pr create --repo vitalharmony/hrse --title x --body "Closes #42"'
        )
        self.assertTrue(_is_denied(result))

    def test_malformed_state_denies(self):
        self.tmp.write_text("not json")
        result = _run_hook(
            'gh pr create --repo vitalharmony/hrse --title x --body "Closes #42"'
        )
        self.assertTrue(_is_denied(result))

    def test_expired_grant_denies(self):
        expired = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.tmp.write_text(json.dumps({
            "H42": {"authorized_at": expired, "expires_at": expired,
                    "targets": [{"action": "gh pr merge", "consumed": False,
                                 "consumed_by": None, "repo": None, "pr_number": None}]},
        }))
        result = _run_hook(
            'gh pr create --repo vitalharmony/hrse --title x --body "Closes #42"'
        )
        self.assertTrue(_is_denied(result))

    def test_grant_for_a_different_issue_denies(self):
        live = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self.tmp.write_text(json.dumps({
            "H99": {"authorized_at": datetime.now(timezone.utc).isoformat(),
                    "expires_at": live,
                    "targets": [{"action": "gh pr merge", "consumed": False,
                                 "consumed_by": None, "repo": None, "pr_number": None}]},
        }))
        result = _run_hook(
            'gh pr create --repo vitalharmony/hrse --title x --body "Closes #42"'
        )
        self.assertTrue(_is_denied(result))

    def test_close_only_grant_no_merge_target_denies(self):
        """A key with only a `gh issue close` target (a legacy/hand-edited
        entry -- authorize() can no longer construct one) does not satisfy
        the exception, which checks specifically for a live merge target."""
        live = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self.tmp.write_text(json.dumps({
            "H42": {"authorized_at": datetime.now(timezone.utc).isoformat(),
                    "expires_at": live,
                    "targets": [{"action": "gh issue close", "consumed": False,
                                 "consumed_by": None, "repo": None, "pr_number": None}]},
        }))
        result = _run_hook(
            'gh pr create --repo vitalharmony/hrse --title x --body "Closes #42"'
        )
        self.assertTrue(_is_denied(result))


class ClosingKeywordAllowedWithLiveBatch(unittest.TestCase):
    """harmonic-forge#612: the one live-BATCH exception."""

    def setUp(self):
        self.tmp = Path(__import__("tempfile").mkdtemp()) / "batch-authorized.json"
        self.patcher = mock.patch.object(bck, "BATCH_STATE_PATH", self.tmp)
        self.patcher.start()
        live = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self.tmp.write_text(json.dumps({
            "H42": {"authorized_at": datetime.now(timezone.utc).isoformat(),
                    "expires_at": live,
                    "targets": [{"action": "gh pr merge", "consumed": False,
                                 "consumed_by": None, "repo": None, "pr_number": None}]},
        }))

    def tearDown(self):
        self.patcher.stop()

    def test_explicit_repo_prefix_allows(self):
        result = _run_hook(
            'gh pr create --repo vitalharmony/hrse --title x '
            '--body "Closes vitalharmony/hrse#42"'
        )
        self.assertEqual(result, {})

    def test_repo_flag_fallback_allows(self):
        result = _run_hook(
            'gh pr create --repo vitalharmony/hrse --title x --body "Closes #42"'
        )
        self.assertEqual(result, {})

    def test_cwd_git_remote_fallback_allows(self):
        with mock.patch.object(bck.subprocess, "run", return_value=subprocess.CompletedProcess(
            [], 0, "https://github.com/vitalharmony/hrse.git\n", "",
        )):
            result = _run_hook(
                'gh issue comment 1 --body "Fixes #42"'
            )
        self.assertEqual(result, {})

    def test_a_second_unauthorized_issue_in_the_same_body_still_denies(self):
        """All referenced issues must be live-authorized, not just one."""
        result = _run_hook(
            'gh pr create --repo vitalharmony/hrse --title x '
            '--body "Closes #42, fixes #99"'
        )
        self.assertTrue(_is_denied(result))


class RepoFlagSpoofingIsRejected(unittest.TestCase):
    """Preclose finding on harmonic-forge#612: a naive command-wide
    `--repo` search matched `--repo ...` text pasted into the PR BODY
    (this house's own PR bodies routinely paste example `gh ... --repo
    ...` command lines) -- when that quoted text appears EARLIER in the
    command string than the real `--repo` flag, `re.search`'s
    leftmost-match picked the fake one, redirecting a bare `#N` to
    whichever repo the quoted text named. A live grant on that OTHER repo
    then read as a false ALLOW for the real PR's actual issue."""

    def setUp(self):
        self.tmp = Path(__import__("tempfile").mkdtemp()) / "batch-authorized.json"
        self.patcher = mock.patch.object(bck, "BATCH_STATE_PATH", self.tmp)
        self.patcher.start()
        live = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        # F42 (harmonic-forge) is live; H42 (hrse) is NOT -- the real PR
        # below targets hrse, so only H42 should ever be consulted.
        self.tmp.write_text(json.dumps({
            "F42": {"authorized_at": datetime.now(timezone.utc).isoformat(),
                    "expires_at": live,
                    "targets": [{"action": "gh pr merge", "consumed": False,
                                 "consumed_by": None, "repo": None, "pr_number": None}]},
        }))

    def tearDown(self):
        self.patcher.stop()

    def test_a_repo_flag_quoted_earlier_in_the_body_is_never_read_as_the_real_flag(self):
        result = _run_hook(
            'gh pr create --title x '
            '--body "Closes #42 (built with --repo vitalharmony/harmonic-forge)" '
            '--repo vitalharmony/hrse'
        )
        self.assertTrue(_is_denied(result))


if __name__ == "__main__":
    unittest.main()
