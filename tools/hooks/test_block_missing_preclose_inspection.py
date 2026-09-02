#!/usr/bin/env python3
"""Tests for block_missing_preclose_inspection.py (hrse#1487).

Target-parsing tests are the same shape as
test_block_data_migration_close.py -- the parser is a straight copy of that
hook's, so the same evasions apply. Decision tests cover the two-signal
logic new to this hook: gate-trail marker vs. preclose-inspection marker.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import block_missing_preclose_inspection as hook  # noqa: E402

HOOK = Path(__file__).resolve().parent / "block_missing_preclose_inspection.py"
REPO = "vitalharmony/hrse"


def bash(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def run_hook(payload: dict) -> dict:
    result = subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(payload),
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout or "{}")


class FindTargetsTests(unittest.TestCase):
    """Same parser as block_data_migration_close.py -- ported evasion set."""

    def assert_target(self, command: str, expected: tuple[str | None, str]) -> None:
        self.assertEqual(hook.find_close_targets(command), [expected])

    def test_issue_close_with_repo(self) -> None:
        self.assert_target(f"gh issue close 1476 --repo {REPO}", (REPO, "1476"))

    def test_api_patch_state_closed(self) -> None:
        self.assert_target(
            f"gh api -X PATCH repos/{REPO}/issues/1476 -f state=closed",
            (REPO, "1476"),
        )

    def test_state_open_is_not_a_close(self) -> None:
        self.assertEqual(hook.find_close_targets(
            f"gh api -X PATCH repos/{REPO}/issues/1476 -f state=open"), [])

    def test_unrelated_commands(self) -> None:
        for command in ("git status", "gh issue view 1476", "gh pr merge 1486"):
            with self.subTest(command=command):
                self.assertEqual(hook.find_close_targets(command), [])


class MarkerTests(unittest.TestCase):
    def test_gate_trail_marker_detected(self) -> None:
        body = "some text\n<!-- l1-post v1; kind=ready-for-l3; sha=abc -->"
        self.assertTrue(hook.has_gate_trail([body]))

    def test_ae_and_sweep_marker_detected(self) -> None:
        body = "<!-- l1-post v1; kind=ae-and-sweep; sha=abc -->"
        self.assertTrue(hook.has_gate_trail([body]))

    def test_handoff_marker_is_not_a_gate_trail(self) -> None:
        """A handoff alone means Plan-First or ordinary handoff -- not that
        Lane 3 ever gated it. Only ready-for-l3/ae/ae-and-sweep count."""
        body = "<!-- l1-post v1; kind=handoff; sha=abc -->"
        self.assertFalse(hook.has_gate_trail([body]))

    def test_preclose_heading_detected_case_insensitive(self) -> None:
        for heading in ("## Preclose-inspection", "### PRECLOSE-INSPECTION", "# preclose-inspection findings"):
            with self.subTest(heading=heading):
                self.assertTrue(hook.has_preclose_inspection([f"{heading}\n\nfindings here"]))

    def test_preclose_mentioned_in_prose_is_not_a_heading(self) -> None:
        """The data-migration hook's own lesson (round 4): prose naming the
        marker must not itself satisfy the check."""
        body = "I should run preclose-inspection before closing this."
        self.assertFalse(hook.has_preclose_inspection([body]))

    def test_no_bodies_means_no_markers(self) -> None:
        self.assertFalse(hook.has_gate_trail([]))
        self.assertFalse(hook.has_preclose_inspection([]))


class DecisionTests(unittest.TestCase):
    CMD = f"gh api -X PATCH repos/{REPO}/issues/1476 -f state=closed"

    def _decide(self, command: str, bodies: list[str] | None,
                repo: str | None = REPO) -> dict:
        with mock.patch.object(hook, "resolve_repo", return_value=repo), \
             mock.patch.object(hook, "comment_bodies", return_value=bodies), \
             mock.patch("sys.stdin", mock.MagicMock()), \
             mock.patch("json.load", return_value=bash(command)), \
             mock.patch("builtins.print") as printed:
            hook.main()
        return json.loads(printed.call_args[0][0])

    def _denied(self, payload: dict) -> bool:
        return (payload.get("hookSpecificOutput") or {}).get(
            "permissionDecision") == "deny"

    def test_no_trail_no_preclose_denies(self) -> None:
        """The hrse#1476 incident, reproduced: a Tooling Exception close
        with no gate trail and no posted review."""
        self.assertTrue(self._denied(self._decide(self.CMD, bodies=["implemented, tests pass"])))

    def test_gate_trail_present_allows_without_preclose(self) -> None:
        """Went through the real 3-lane cycle -- a different gate covers it."""
        self.assertFalse(self._denied(self._decide(
            self.CMD, bodies=["<!-- l1-post v1; kind=ready-for-l3; sha=x -->"])))

    def test_preclose_present_allows(self) -> None:
        self.assertFalse(self._denied(self._decide(
            self.CMD, bodies=["## Preclose-inspection\n\nNo issues found."])))

    def test_deny_names_the_issue_and_hrse_1487(self) -> None:
        payload = self._decide(self.CMD, bodies=[])
        reason = payload["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("1476", reason)
        self.assertIn("hrse#1487", reason)
        self.assertIn("preclose-inspection", reason.lower())

    def test_unresolvable_repo_allows(self) -> None:
        self.assertFalse(self._denied(self._decide(self.CMD, bodies=[], repo=None)))

    def test_unreadable_comments_fail_open(self) -> None:
        self.assertFalse(self._denied(self._decide(self.CMD, bodies=None)))

    def test_docs_naming_the_heading_do_not_grant_it(self) -> None:
        """Same lesson as block_data_migration_close.py round 4: quoting the
        marker's format in a comment must not itself satisfy the gate --
        this body describes the heading without actually being one."""
        body = "Docs say a heading matching '## Preclose-inspection' is required."
        self.assertTrue(self._denied(self._decide(self.CMD, bodies=[body])))


class ProcessTests(unittest.TestCase):
    def test_non_bash_allowed(self) -> None:
        self.assertEqual(run_hook({"tool_name": "Read"}), {})

    def test_malformed_payload_allowed(self) -> None:
        result = subprocess.run([sys.executable, str(HOOK)], input="not json",
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout or "{}"), {})

    def test_unrelated_bash_allowed(self) -> None:
        self.assertEqual(run_hook(bash("git status")), {})


class CommentBodiesTests(unittest.TestCase):
    def test_parses_one_json_string_per_line(self) -> None:
        raw = '"first comment"\n"second comment"\n'
        with mock.patch.object(hook, "_gh", return_value=raw):
            self.assertEqual(hook.comment_bodies(REPO, "1476"),
                             ["first comment", "second comment"])

    def test_returns_none_on_gh_failure(self) -> None:
        with mock.patch.object(hook, "_gh", return_value=None):
            self.assertIsNone(hook.comment_bodies(REPO, "1476"))


if __name__ == "__main__":
    unittest.main()
