#!/usr/bin/env python3
"""Tests for block_closing_keywords.py (harmonic-forge#84/#93).

harmonic-forge#911 retired #612's live-BATCH exception: a closing keyword is
denied whatever the BATCH state, so these tests seed a live grant to prove it
no longer opens anything.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
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


class ClosingKeywordAlwaysDenied(unittest.TestCase):
    def test_closes_keyword_denies(self):
        result = _run_hook(
            'gh pr create --repo vitalharmony/hrse --title x --body "Closes #42"'
        )
        self.assertTrue(_is_denied(result))
        self.assertIn("harmonic-forge#911", result["systemMessage"])

    def test_fixes_keyword_denies(self):
        result = _run_hook(
            'gh pr create --repo vitalharmony/hrse --title x --body "Fixes #42"'
        )
        self.assertTrue(_is_denied(result))

    def test_comment_with_closing_keyword_denies(self):
        result = _run_hook('gh issue comment 1 --body "Resolves vitalharmony/hrse#42"')
        self.assertTrue(_is_denied(result))


KEYWORD = "Clo" + "ses"  # built so no shell text spells a closing keyword


def _run_hook_process(command: str, home: Path) -> dict:
    """The hook as Claude Code runs it: a fresh process, HOME pointed at
    `home`, so any module-level path bound at import resolves under it."""
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    env = {**os.environ, "HOME": str(home)}
    result = subprocess.run([sys.executable, str(HOOK_DIR / "block_closing_keywords.py")],
                            input=payload, capture_output=True, text=True, env=env, check=True)
    return json.loads(result.stdout)


class LiveBatchGrantOpensNothing(unittest.TestCase):
    """harmonic-forge#911: a live BATCH grant covering the issue's merge
    used to allow a closing keyword (#612). With a live grant seeded where
    the old allow path read it, the hook must still deny."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        live = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        state = self.home / ".claude" / "state" / "batch-authorized.json"
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({
            "H42": {"authorized_at": datetime.now(timezone.utc).isoformat(),
                    "expires_at": live,
                    "targets": [{"action": "gh pr merge", "consumed": False,
                                 "consumed_by": None, "repo": None, "pr_number": None}]},
        }))

    def test_live_grant_with_explicit_repo_still_denies(self):
        result = _run_hook_process(
            "gh pr create --repo vitalharmony/hrse --title x "
            f'--body "{KEYWORD} vitalharmony/hrse#42"', self.home)
        self.assertTrue(_is_denied(result))

    def test_live_grant_with_repo_flag_still_denies(self):
        result = _run_hook_process(
            f'gh pr create --repo vitalharmony/hrse --title x --body "{KEYWORD} #42"',
            self.home)
        self.assertTrue(_is_denied(result))


class EveryReferenceFormAndSurfaceIsDenied(unittest.TestCase):
    """harmonic-forge#911 preclose: the URL reference form, and a PR created
    through the REST API, both passed the hook before."""

    def test_issue_url_form_denies(self):
        result = _run_hook(
            "gh pr create --repo vitalharmony/hrse --title x "
            f'--body "{KEYWORD} https://github.com/vitalharmony/hrse/issues/42"')
        self.assertTrue(_is_denied(result))

    def test_rest_api_pr_creation_denies(self):
        result = _run_hook(
            "gh api -X POST repos/vitalharmony/hrse/pulls -f title=x -f head=b "
            f"-f base=main -f body='{KEYWORD} #42'")
        self.assertTrue(_is_denied(result))

    def test_rest_api_without_a_keyword_is_untouched(self):
        result = _run_hook("gh api repos/vitalharmony/hrse/issues/42")
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
