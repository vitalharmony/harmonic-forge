#!/usr/bin/env python3
"""Tests for block_undetermined_phase_close.py (harmonic-forge#642 slice 1).

Modeled on test_block_missing_preclose_inspection.py's shape: parse tests
against the module's own classify_* wrappers, decision tests via mocked
gh-backed helpers, and a keyword-regex parity pin against
`tools/gh/block_closing_keywords.py`.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gh"))

import block_undetermined_phase_close as hook  # noqa: E402

HOOK = Path(__file__).resolve().parent / "block_undetermined_phase_close.py"
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


def denied(payload: dict) -> bool:
    return (payload.get("hookSpecificOutput") or {}).get(
        "permissionDecision") == "deny"


class KeywordRegexParityTests(unittest.TestCase):
    def test_byte_identical_to_block_closing_keywords(self) -> None:
        import block_closing_keywords as canonical  # noqa: PLC0415

        self.assertEqual(hook.CLOSING_KEYWORD.pattern, canonical.CLOSING_KEYWORD.pattern)
        self.assertEqual(hook.CLOSING_KEYWORD.flags, canonical.CLOSING_KEYWORD.flags)


class ParseTests(unittest.TestCase):
    def test_issue_close_cli(self) -> None:
        self.assertEqual(
            hook.classify_issue_close_target(["gh", "issue", "close", "191",
                                               "--repo", REPO]),
            (REPO, "191"))

    def test_issue_close_repo_equals_is_ambiguous(self) -> None:
        # harmonic-forge#1339 operator ruling: no flag grammar; the `=` form is
        # not canonical, so it is AMBIGUOUS.
        import batch_auth  # noqa: PLC0415
        target = hook.classify_issue_close_target(["gh", "issue", "close", "191",
                                                    f"--repo={REPO}"])
        self.assertIs(target[0], batch_auth.AMBIGUOUS)

    def test_close_with_comment_is_ambiguous(self) -> None:
        # harmonic-forge#1339 operator ruling: no flag grammar, so a repo flag
        # outside the canonical form is AMBIGUOUS.
        import batch_auth  # noqa: PLC0415
        target = hook.classify_issue_close_target(["gh", "issue", "close", "5", "--repo",
                                                    "o/r", "--comment", "x"])
        self.assertIs(target[0], batch_auth.AMBIGUOUS)

    def test_repo_glued_inside_another_flags_value_is_ambiguous(self) -> None:
        import batch_auth  # noqa: PLC0415
        target = hook.classify_pr_merge_target("gh pr merge 7 --body -Rx --repo o/r".split())
        self.assertIs(target[0], batch_auth.AMBIGUOUS)

    def test_canonical_forms_still_resolve(self) -> None:
        self.assertEqual(
            hook.classify_pr_merge_target(
                "gh pr merge 7 --repo o/r --squash --match-head-commit abc".split()),
            ("o/r", "7"))
        self.assertEqual(
            hook.classify_issue_close_target(
                "gh api repos/o/r/issues/5 -X PATCH -f state=closed".split()),
            ("o/r", "5"))

    def test_differing_repo_flags_are_ambiguous(self) -> None:
        import batch_auth  # noqa: PLC0415
        target = hook.classify_issue_close_target("gh -R a/b issue close 5 --repo c/d".split())
        self.assertIs(target[0], batch_auth.AMBIGUOUS)

    def test_issue_close_url_form(self) -> None:
        self.assertEqual(
            hook.classify_issue_close_target(
                ["gh", "issue", "close", f"https://github.com/{REPO}/issues/191"]),
            (REPO, "191"))

    def test_rest_patch_close(self) -> None:
        self.assertEqual(
            hook.classify_issue_close_target(
                ["gh", "api", "-X", "PATCH", f"repos/{REPO}/issues/191",
                 "-f", "state=closed"]),
            (REPO, "191"))

    def test_pr_merge_cli(self) -> None:
        self.assertEqual(
            hook.classify_pr_merge_target(["gh", "pr", "merge", "1813",
                                            "--repo", REPO, "--squash"]),
            (REPO, "1813"))

    def test_rest_put_merge(self) -> None:
        self.assertEqual(
            hook.classify_pr_merge_target(
                ["gh", "api", "-X", "PUT", f"repos/{REPO}/pulls/1813/merge"]),
            (REPO, "1813"))

    def test_unrelated_commands_have_no_target(self) -> None:
        for tokens in (["git", "status"], ["gh", "issue", "view", "191"],
                       ["gh", "pr", "view", "1813"]):
            with self.subTest(tokens=tokens):
                self.assertIsNone(hook.classify_issue_close_target(tokens))
                self.assertIsNone(hook.classify_pr_merge_target(tokens))


class ExemptionTests(unittest.TestCase):
    def test_reason_not_planned(self) -> None:
        self.assertTrue(hook.is_exempt_close(
            ["gh", "issue", "close", "191", "--reason", "not planned"]))

    def test_reason_duplicate(self) -> None:
        self.assertTrue(hook.is_exempt_close(
            ["gh", "issue", "close", "191", "-r", "duplicate"]))

    def test_reason_completed_not_exempt(self) -> None:
        self.assertFalse(hook.is_exempt_close(
            ["gh", "issue", "close", "191", "--reason", "completed"]))

    def test_duplicate_of_flag(self) -> None:
        self.assertTrue(hook.is_exempt_close(
            ["gh", "issue", "close", "191", "--duplicate-of", "42"]))

    def test_rest_state_reason_not_planned(self) -> None:
        self.assertTrue(hook.is_exempt_close(
            ["gh", "api", "-X", "PATCH", "repos/o/r/issues/191",
             "-f", "state_reason=not_planned"]))

    def test_no_reason_not_exempt(self) -> None:
        self.assertFalse(hook.is_exempt_close(
            ["gh", "issue", "close", "191"]))


class ClosingKeywordScanTests(unittest.TestCase):
    def test_bare_hash_resolves_to_this_repo(self) -> None:
        self.assertEqual(hook._closing_keyword_targets("Closes #191", REPO),
                          {"191"})

    def test_url_form(self) -> None:
        text = f"Closes https://github.com/{REPO}/issues/191"
        self.assertEqual(hook._closing_keyword_targets(text, REPO), {"191"})

    def test_cross_repo_reference_excluded(self) -> None:
        self.assertEqual(
            hook._closing_keyword_targets("Closes other/repo#191", REPO), set())

    def test_no_keyword_no_target(self) -> None:
        self.assertEqual(hook._closing_keyword_targets("see #191", REPO), set())


class DecisionTests(unittest.TestCase):
    CMD = f"gh issue close 191 --repo {REPO}"

    def _decide(self, command: str, labels: set[str] | None,
                repo: str | None = REPO,
                since: str | None = "2026-09-10T00:00:00Z",
                has_evidence: bool | None = True,
                has_blocker: bool | None = True,
                closed_issues=None) -> dict:
        if closed_issues is None:
            closed_issues = {"191"}
        with mock.patch.object(hook, "resolve_repo", return_value=repo), \
             mock.patch.object(hook, "labels_for", return_value=labels), \
             mock.patch.object(hook, "_latest_label_event_time", return_value=since), \
             mock.patch.object(hook, "_has_evidence_comment", return_value=has_evidence), \
             mock.patch.object(hook, "_has_open_blocker", return_value=has_blocker), \
             mock.patch.object(hook, "issues_closed_by_pr", return_value=closed_issues), \
             mock.patch("sys.stdin", mock.MagicMock()), \
             mock.patch("json.load", return_value=bash(command)), \
             mock.patch("builtins.print") as printed:
            hook.main()
        return json.loads(printed.call_args[0][0])

    def test_unrelated_issue_allowed(self) -> None:
        self.assertFalse(denied(self._decide(self.CMD, labels={"bug"})))

    def test_neither_label_denies(self) -> None:
        self.assertTrue(denied(self._decide(self.CMD, labels={"phase"})))

    def test_both_labels_deny(self) -> None:
        self.assertTrue(denied(self._decide(
            self.CMD, labels={"phase", "shipped", "shipped-inert"})))

    def test_shipped_with_evidence_allows(self) -> None:
        self.assertFalse(denied(self._decide(
            self.CMD, labels={"phase", "shipped"}, has_evidence=True)))

    def test_shipped_without_evidence_denies(self) -> None:
        self.assertTrue(denied(self._decide(
            self.CMD, labels={"phase", "shipped"}, has_evidence=False)))

    def test_shipped_inert_with_open_blocker_allows(self) -> None:
        self.assertFalse(denied(self._decide(
            self.CMD, labels={"epic", "shipped-inert"}, has_blocker=True)))

    def test_shipped_inert_without_open_blocker_denies(self) -> None:
        self.assertTrue(denied(self._decide(
            self.CMD, labels={"epic", "shipped-inert"}, has_blocker=False)))

    def test_unreadable_labels_fail_open(self) -> None:
        self.assertFalse(denied(self._decide(self.CMD, labels=None)))

    def test_unresolvable_repo_allows(self) -> None:
        self.assertFalse(denied(self._decide(self.CMD, labels={"phase"}, repo=None)))

    def test_deny_names_hrse_195(self) -> None:
        payload = self._decide(self.CMD, labels={"phase"})
        reason = payload["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("hrse#195", reason)
        self.assertIn("191", reason)

    def test_pr_merge_via_keyword_denies(self) -> None:
        cmd = f"gh pr merge 1813 --repo {REPO} --squash"
        self.assertTrue(denied(self._decide(cmd, labels={"phase"})))

    def _main_on(self, command: str):
        with mock.patch.object(hook, "resolve_repo") as resolve, \
             mock.patch.object(hook, "labels_for") as labels, \
             mock.patch.object(hook, "issues_closed_by_pr") as pr_read, \
             mock.patch("sys.stdin", mock.MagicMock()), \
             mock.patch("json.load", return_value=bash(command)), \
             mock.patch("builtins.print") as printed:
            hook.main()
        return resolve, labels, pr_read, json.loads(printed.call_args[0][0])

    def test_ambiguous_targets_deny_and_never_read_any_repo(self) -> None:
        # harmonic-forge#1339 operator ruling: each must deny naming the
        # ambiguity, and never phase-check cwd's or a guessed repo.
        for command in ("gh -R a/b issue close 5 --repo c/d",
                        "gh issue close 5 --repo o/r --comment x",
                        "gh pr merge 5 -r -R other/repo",
                        "gh api repos/vitalharmony/hrse/issues/5 -X PATCH -f state=closed "
                        "--template -Rvitalharmony/harmonic-forge"):
            with self.subTest(command=command):
                resolve, labels, pr_read, payload = self._main_on(command)
                resolve.assert_not_called()
                labels.assert_not_called()
                pr_read.assert_not_called()
                self.assertTrue(denied(payload))
                reason = payload["hookSpecificOutput"]["permissionDecisionReason"]
                self.assertIn("ambiguous target repository", reason)

    def test_canonical_merge_and_api_close_are_checked_against_their_repo(self) -> None:
        for command, want in (
                ("gh pr merge 7 --repo o/r --squash --match-head-commit abc", ("o/r", "7")),
                ("gh api repos/o/r/issues/5 -X PATCH -f state=closed", ("o/r", "5"))):
            with self.subTest(command=command), \
                 mock.patch.object(hook, "resolve_repo", side_effect=lambda r, c: r), \
                 mock.patch.object(hook, "labels_for", return_value=set()) as labels, \
                 mock.patch.object(hook, "issues_closed_by_pr", return_value=set()) as pr_read, \
                 mock.patch("sys.stdin", mock.MagicMock()), \
                 mock.patch("json.load", return_value=bash(command)), \
                 mock.patch("builtins.print"):
                hook.main()
                called = pr_read if "merge" in command else labels
                self.assertEqual(called.call_args[0][:2], want)

    def test_pr_merge_no_keyword_target_allows(self) -> None:
        cmd = f"gh pr merge 1813 --repo {REPO} --squash"
        self.assertFalse(denied(self._decide(cmd, labels={"phase"}, closed_issues=set())))


class ProcessTests(unittest.TestCase):
    def test_non_bash_allowed(self) -> None:
        self.assertEqual(run_hook({"tool_name": "Read"}), {})

    def test_malformed_payload_allowed(self) -> None:
        result = subprocess.run(
            [sys.executable, str(HOOK)], input="not json",
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(json.loads(result.stdout), {})

    def test_unrelated_bash_allowed(self) -> None:
        self.assertEqual(run_hook(bash("git status")), {})

    def test_exempt_close_makes_no_gh_call(self) -> None:
        with mock.patch.object(hook, "labels_for") as labels_mock, \
             mock.patch("sys.stdin", mock.MagicMock()), \
             mock.patch("json.load", return_value=bash(
                 f"gh issue close 191 --repo {REPO} --reason 'not planned'")), \
             mock.patch("builtins.print"):
            hook.main()
        labels_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
