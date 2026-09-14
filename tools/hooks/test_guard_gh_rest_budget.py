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
    """In-process check via `_check_segment`, with `override_present` mocked
    so tests don't touch the real filesystem override path. The hook only
    peeks at the override (never consumes it) -- the shim is the sole
    consumer -- so this must mock `override_present`, not `consume_override`."""
    import gh_scan_patterns
    with mock.patch.object(gh_scan_patterns, "override_present", return_value=override):
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

    def test_mkdir_on_override_path_denied(self):
        """harmonic-forge#650 preclose-check: `mkdir` was not on the old
        write-binary allowlist, so `mkdir -p` on the override path passed
        clean and created a valid-looking (but bogus) grant."""
        self.assertIsNotNone(_check("mkdir -p ~/.cache/harmonic-forge/gh_scan_override"))

    def test_env_prefixed_touch_denied(self):
        """harmonic-forge#650 preclose-check: `env touch ...` read `env` as
        `tokens[0]`, which was never on the write-binary allowlist."""
        self.assertIsNotNone(_check("env FOO=bar touch ~/.cache/harmonic-forge/gh_scan_override"))

    def test_pathlib_touch_call_denied(self):
        """harmonic-forge#650 preclose-check: `.touch()` on a `pathlib.Path`
        matched none of the old write-shaped substrings (`open(`, `Path(`,
        `write_text`, `os.replace`)."""
        self.assertIsNotNone(_check(
            "python3 -c \"import pathlib; "
            "pathlib.Path.home().joinpath('.cache/harmonic-forge/gh_scan_override').touch()\""
        ))

    def test_cat_with_redirect_into_override_still_denied(self):
        self.assertIsNotNone(_check("cat x > ~/.cache/harmonic-forge/gh_scan_override"))

    def test_reading_via_stat_is_allowed(self):
        self.assertIsNone(_check("stat ~/.cache/harmonic-forge/gh_scan_override"))


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

    def test_ac6_list_literal_form_denied(self):
        """The exact AC6 example: a Python list literal, not a whitespace
        string, so `issue` and `list` are separated by `","` rather than a
        space (harmonic-forge#650 preclose-check finding)."""
        self.assertIsNotNone(_check(
            'python3 -c \'import subprocess; subprocess.run(["gh","issue","list",'
            '"--repo","vitalharmony/hrse"])\''
        ))


class RawHttpHeredocDenial(unittest.TestCase):
    """harmonic-forge#650 preclose-check: `command_segments()` replaces a
    heredoc body with a placeholder so its prose is never tokenized, which
    means no segment-level check can see a raw HTTP call inside one. The
    fix scans the RAW command text separately, in `main()`."""

    def test_heredoc_urllib_to_github_denied(self):
        decision = _run_subprocess(
            "python3 - <<'EOF'\n"
            "import urllib.request\n"
            "urllib.request.urlopen('https://api.github.com/repos/o/r/issues')\n"
            "EOF"
        )
        self.assertEqual(
            decision.get("hookSpecificOutput", {}).get("permissionDecision"), "deny"
        )

    def test_heredoc_requests_to_github_denied(self):
        decision = _run_subprocess(
            "python3 - <<'EOF'\n"
            "import requests\n"
            "requests.get('https://api.github.com/repos/o/r/issues')\n"
            "EOF"
        )
        self.assertEqual(
            decision.get("hookSpecificOutput", {}).get("permissionDecision"), "deny"
        )

    def test_ordinary_heredoc_with_no_api_call_is_allowed(self):
        decision = _run_subprocess("cat <<'EOF'\nsome ordinary text, no API calls here\nEOF")
        self.assertNotIn("hookSpecificOutput", decision)


class GhBasenameNormalization(unittest.TestCase):
    """harmonic-forge#650 preclose-check: `stripped[0] == "gh"` (exact
    string) let `/usr/bin/gh issue list` straight through."""

    def test_absolute_path_gh_scan_denied(self):
        self.assertIsNotNone(_check("/usr/bin/gh issue list"))


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
