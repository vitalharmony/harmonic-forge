"""harmonic-forge#650 -- the REST-core-budget PreToolUse guard.

Modeled on test_block_raw_board_scan.py: subprocess the hook with a JSON
payload on stdin, parse its JSON stdout.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

HOOK = Path(__file__).parent / "guard_gh_rest_budget.py"

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "gh"))
import guard_gh_rest_budget as guard  # noqa: E402


def _run_subprocess(command: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"tool_input": {"command": command}}),
        capture_output=True, text=True,
    )
    return json.loads(result.stdout or "{}")


def _check(command: str, override: bool = False) -> str | None:
    """In-process check via `_check_segment`, with `consume_override` mocked
    so tests don't touch the real filesystem override path."""
    import gh_scan_patterns
    with mock.patch.object(gh_scan_patterns, "consume_override", return_value=override):
        for segment in guard.command_segments(command):
            reason = guard._check_segment(segment)
            if reason is not None:
                return reason
    return None


class ScanDenials(unittest.TestCase):
    def test_gh_issue_list_denied(self):
        self.assertIsNotNone(_check("gh issue list"))

    def test_gh_pr_list_denied(self):
        self.assertIsNotNone(_check("gh pr list"))

    def test_gh_search_denied(self):
        self.assertIsNotNone(_check("gh search issues foo"))

    def test_gh_project_item_list_denied(self):
        self.assertIsNotNone(_check("gh project item-list 1 --owner x"))

    def test_override_present_allows_the_scan(self):
        self.assertIsNone(_check("gh issue list", override=True))

    def test_single_issue_read_is_allowed(self):
        self.assertIsNone(_check("gh api repos/o/r/issues/123"))

    def test_non_belt_mode_watch_lane_posts_is_allowed(self):
        self.assertIsNone(
            _check("python3 ~/harmonic-forge/tools/gh/watch_lane_posts.py --repo o/r")
        )


class WatchLanePostsIntervalFloor(unittest.TestCase):
    def test_belt_mode_under_floor_interval_denied(self):
        reason = _check(
            "python3 ~/harmonic-forge/tools/gh/watch_lane_posts.py "
            "--all-worktrees --queue-for l1 --watch l2 --interval 60"
        )
        self.assertIsNotNone(reason)

    def test_belt_mode_missing_interval_denied(self):
        reason = _check(
            "python3 ~/harmonic-forge/tools/gh/watch_lane_posts.py "
            "--all-worktrees --queue-for l1 --watch l2"
        )
        self.assertIsNotNone(reason)

    def test_belt_mode_at_floor_interval_allowed(self):
        reason = _check(
            "python3 ~/harmonic-forge/tools/gh/watch_lane_posts.py "
            "--all-worktrees --queue-for l1 --watch l2 --interval 300"
        )
        self.assertIsNone(reason)

    def test_non_belt_mode_missing_interval_allowed(self):
        """Conservative: if we can't tell it's belt mode, don't deny."""
        reason = _check("python3 ~/harmonic-forge/tools/gh/watch_lane_posts.py --repo o/r")
        self.assertIsNone(reason)


class OverrideFileWriteDenial(unittest.TestCase):
    def test_touch_override_file_denied(self):
        self.assertIsNotNone(_check("touch ~/.cache/harmonic-forge/gh_scan_override"))

    def test_redirect_into_override_file_denied(self):
        self.assertIsNotNone(_check("echo x > ~/.cache/harmonic-forge/gh_scan_override"))

    def test_python_write_to_override_file_denied(self):
        self.assertIsNotNone(_check(
            'python3 -c "open(\'/home/x/.cache/harmonic-forge/gh_scan_override\', \'w\').close()"'
        ))

    def test_reading_override_file_is_allowed(self):
        self.assertIsNone(_check("cat ~/.cache/harmonic-forge/gh_scan_override"))


class InlineScriptDenial(unittest.TestCase):
    def test_api_github_com_denied(self):
        self.assertIsNotNone(_check(
            'python3 -c "import requests; requests.get(\'https://api.github.com/repos/o/r\')"'
        ))

    def test_fetch_item_list_ttl_zero_denied(self):
        self.assertIsNotNone(_check(
            "python3 -c \"import item_list_cache; item_list_cache.fetch_item_list('1', ttl=0)\""
        ))

    def test_fetch_item_list_with_real_ttl_allowed(self):
        self.assertIsNone(_check(
            "python3 -c \"import item_list_cache; item_list_cache.fetch_item_list('1', ttl=120)\""
        ))

    def test_gh_list_literal_denied(self):
        self.assertIsNotNone(_check(
            'python3 -c "import subprocess; subprocess.run(\'gh issue list\'.split())"'
        ))


class HookOutputShape(unittest.TestCase):
    def test_denied_command_returns_deny(self):
        out = _run_subprocess("gh issue list")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_deny_reason_names_the_override_unlock(self):
        out = _run_subprocess("gh issue list")
        self.assertIn(
            "gh_scan_override", out["hookSpecificOutput"]["permissionDecisionReason"]
        )

    def test_allowed_command_returns_empty_object(self):
        self.assertEqual(_run_subprocess("gh api repos/o/r/issues/123"), {})

    def test_malformed_payload_fails_open_and_visible(self):
        result = subprocess.run(
            [sys.executable, str(HOOK)], input="not json",
            capture_output=True, text=True,
        )
        out = json.loads(result.stdout)
        self.assertIn("malformed", out["systemMessage"])


if __name__ == "__main__":
    unittest.main()
