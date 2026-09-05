"""harmonic-forge#468 — the raw-board-scan guard.

The measured fact this hook exists for: `fetch_item_list` had **zero**
production callers in either repo, yet the GraphQL quota went to zero twice.
The 5000-item cache files that named the 2026-09-04 incident were written by an
agent running ad-hoc Python — code that does not exist until the moment it runs,
which no library default can reach in advance. A shell command is where that
becomes visible.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

HOOK = Path(__file__).parent / "block_raw_board_scan.py"

sys.path.insert(0, str(Path(__file__).parent))
import block_raw_board_scan as guard  # noqa: E402


def _run(command: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"tool_input": {"command": command}}),
        capture_output=True, text=True,
    )
    return json.loads(result.stdout or "{}")


class Detection(unittest.TestCase):
    def test_the_raw_scan_is_flagged(self):
        for command in (
            "gh project item-list 1 --owner vitalharmony --format json",
            "gh project item-list 3 --owner x --limit 5000",
            "echo hi && gh project item-list 3 --owner x",
            "cd /tmp; gh project item-list 1 --owner y | jq .",
        ):
            with self.subTest(command=command):
                self.assertTrue(guard.is_raw_board_scan(command), command)

    def test_other_project_subcommands_are_not_flagged(self):
        """`item-add`, `item-edit` and `field-list` are cheap and are how the
        board is legitimately written. Flagging them would make the guard noise
        the operator learns to click through."""
        for command in (
            "gh project item-add 1 --owner x --url https://github.com/a/b/issues/1",
            "gh project item-edit --id X --field-id Y --project-id Z",
            "gh project field-list 3 --owner x",
            "gh project view 1 --owner x",
        ):
            with self.subTest(command=command):
                self.assertFalse(guard.is_raw_board_scan(command), command)

    def test_unrelated_commands_are_not_flagged(self):
        for command in ("gh issue list", "gh api graphql -f query=x", "ls", ""):
            with self.subTest(command=command):
                self.assertFalse(guard.is_raw_board_scan(command))

    def test_the_mandated_path_is_not_flagged(self):
        """The guard is on the COMMAND, not the caller. Invoking the module is
        the whole point of the mandate and must stay frictionless."""
        for command in (
            "python3 -c 'import item_list_cache; item_list_cache.get_board_items(\"1\")'",
            "python3 tools/gh/board_report.py",
        ):
            with self.subTest(command=command):
                self.assertFalse(guard.is_raw_board_scan(command))


class Output(unittest.TestCase):
    def test_a_flagged_command_asks_rather_than_denies(self):
        """A genuine full-board question exists — drift checks, delta syncs —
        and the operator can approve it. What must not happen is it passing
        unnoticed."""
        out = _run("gh project item-list 1 --owner vitalharmony")
        self.assertEqual(
            out["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_the_reason_names_the_cheaper_paths(self):
        """A guard that only says no sends the caller looking for a way around
        it."""
        reason = _run("gh project item-list 1 --owner v")["hookSpecificOutput"][
            "permissionDecisionReason"]
        self.assertIn("fetch_issue_field", reason)
        self.assertIn("get_board_items", reason)
        self.assertIn("fetch_full_board", reason)

    def test_an_allowed_command_produces_an_empty_object(self):
        self.assertEqual(_run("gh issue list"), {})

    def test_a_malformed_payload_is_visible_not_silent(self):
        """harmonic-forge#440's posture: a guard that did not run must not be
        indistinguishable from one that found nothing."""
        result = subprocess.run(
            [sys.executable, str(HOOK)], input="not json",
            capture_output=True, text=True,
        )
        self.assertIn("malformed", json.loads(result.stdout)["systemMessage"])


class Wiring(unittest.TestCase):
    """harmonic-forge#367's defect was wiring one repo and not the other."""

    #: (checkout, the branch that wires it) — named per repo, because the
    #: two halves are separate PRs on separate repos and a message pointing at
    #: the wrong one sends the reader to the wrong place.
    _REPOS = (
        (Path.home() / "Harmonic_Projects" / "harmonic-forge",
         "l2/f468-mandated-board-read"),
        (Path.home() / "Harmonic_Projects" / "HRSE2",
         "feat/f468-wire-board-scan-guard"),
    )

    def test_wired_in_both_repos(self):
        """Asserted, not assumed — harmonic-forge#367 wired one repo and not the
        other, and the gap was invisible.

        **This is RED for hrse until its companion PR lands, deliberately.**
        The hrse half of this change is a separate branch on a separate repo
        (`feat/f468-wire-board-scan-guard`), so between the two merges the guard
        is live in forge and absent in hrse. A softer check that passed in that
        window would be the silent-skip shape this project has already paid for
        twice; a red test naming the file is the honest state of a two-repo
        change mid-landing. Same convention as
        `test_compaction_marker.Wiring.test_wired_in_the_sibling_repo`.
        """
        for repo, branch in self._REPOS:
            settings = repo / ".claude" / "settings.json"
            if not settings.exists():
                self.skipTest(f"{settings} not present in this checkout")
            wired = any(
                "block_raw_board_scan" in hook.get("command", "")
                for entry in json.loads(settings.read_text())
                             .get("hooks", {}).get("PreToolUse", [])
                for hook in entry.get("hooks", [])
            )
            self.assertTrue(
                wired,
                f"block_raw_board_scan not wired in {settings} — wiring one repo "
                f"and not the other is harmonic-forge#367's defect. Land "
                f"`{branch}`.",
            )


if __name__ == "__main__":
    unittest.main()
