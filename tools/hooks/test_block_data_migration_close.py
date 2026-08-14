#!/usr/bin/env python3
"""Tests for block_data_migration_close.py (hrse#859).

Every evasion the first implementation's review found is a test here.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import block_data_migration_close as hook  # noqa: E402

HOOK = Path(__file__).resolve().parent / "block_data_migration_close.py"
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
    def assert_target(self, command: str, expected: tuple[str | None, str]) -> None:
        self.assertEqual(hook.find_close_targets(command), [expected])

    def test_issue_close_with_repo(self) -> None:
        self.assert_target(f"gh issue close 849 --repo {REPO}", (REPO, "849"))

    def test_issue_close_short_repo_flag(self) -> None:
        """-R is gh's documented short form; the first version missed it."""
        self.assert_target(f"gh issue close 849 -R {REPO}", (REPO, "849"))

    def test_issue_close_repo_equals_form(self) -> None:
        self.assert_target(f"gh issue close 849 --repo={REPO}", (REPO, "849"))

    def test_comment_containing_a_number_does_not_hijack(self) -> None:
        """The live bypass found in review: --comment 'closes 219 rows'."""
        self.assert_target(
            f"gh issue close 849 --repo {REPO} --comment 'closes 219 rows'",
            (REPO, "849"),
        )

    def test_quoted_issue_number(self) -> None:
        self.assert_target(f"gh issue close '#849' --repo {REPO}", (REPO, "849"))

    def test_repo_omitted_yields_none(self) -> None:
        self.assert_target("gh issue close 849", (None, "849"))

    def test_flag_value_is_not_mistaken_for_the_issue(self) -> None:
        self.assert_target(
            f"gh issue close --repo {REPO} --comment 219 849", (REPO, "849"),
        )

    def test_api_patch_state_closed(self) -> None:
        self.assert_target(
            f"gh api -X PATCH repos/{REPO}/issues/849 -f state=closed",
            (REPO, "849"),
        )

    def test_api_method_long_flag(self) -> None:
        """--method PATCH is documented gh syntax; first version missed it."""
        self.assert_target(
            f"gh api --method PATCH repos/{REPO}/issues/849 -f state=closed",
            (REPO, "849"),
        )

    def test_api_quoted_state_value(self) -> None:
        self.assert_target(
            f"gh api -X PATCH repos/{REPO}/issues/849 -f state='closed'",
            (REPO, "849"),
        )

    def test_api_flag_order_reversed(self) -> None:
        self.assert_target(
            f"gh api -f state=closed -X PATCH repos/{REPO}/issues/849",
            (REPO, "849"),
        )

    def test_compound_command_returns_every_target(self) -> None:
        got = hook.find_close_targets(
            f"gh issue close 849 --repo {REPO}; gh issue close 850 --repo {REPO}"
        )
        self.assertEqual(got, [(REPO, "849"), (REPO, "850")])

    def test_state_open_is_not_a_close(self) -> None:
        self.assertEqual(hook.find_close_targets(
            f"gh api -X PATCH repos/{REPO}/issues/849 -f state=open"), [])

    def test_reopen_is_not_a_close(self) -> None:
        self.assertEqual(hook.find_close_targets("gh issue reopen 849"), [])

    def test_unrelated_commands(self) -> None:
        for command in ("git status", "gh issue view 849", "echo gh issue close 1"):
            with self.subTest(command=command):
                self.assertEqual(hook.find_close_targets(command), [])

    def test_unbalanced_quotes_fail_open(self) -> None:
        self.assertIsNone(hook.find_close_targets("gh issue close 849 --comment 'x"))


class DecisionTests(unittest.TestCase):
    CMD = f"gh api -X PATCH repos/{REPO}/issues/849 -f state=closed"

    def _decide(self, command: str, labels: set[str] | None,
                repo: str | None = REPO) -> dict:
        with mock.patch.object(hook, "resolve_repo", return_value=repo), \
             mock.patch.object(hook, "labels_for", return_value=labels), \
             mock.patch("sys.stdin", mock.MagicMock()), \
             mock.patch("json.load", return_value=bash(command)), \
             mock.patch("builtins.print") as printed:
            hook.main()
        return json.loads(printed.call_args[0][0])

    def _denied(self, payload: dict) -> bool:
        return (payload.get("hookSpecificOutput") or {}).get(
            "permissionDecision") == "deny"

    def test_labelled_without_evidence_denies(self) -> None:
        self.assertTrue(self._denied(
            self._decide(self.CMD, labels={hook.LABEL})))

    def test_deny_sets_permission_decision_reason(self) -> None:
        """The model reads permissionDecisionReason, not systemMessage."""
        payload = self._decide(self.CMD, labels={hook.LABEL})
        reason = payload["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn(hook.EXECUTED_LABEL, reason)
        self.assertIn("849", reason)

    def test_labelled_with_evidence_allows(self) -> None:
        self.assertFalse(self._denied(
            self._decide(self.CMD, labels={hook.LABEL, hook.EXECUTED_LABEL})))

    def test_unlabelled_allows(self) -> None:
        self.assertFalse(self._denied(
            self._decide(self.CMD, labels=set())))

    def test_unresolvable_repo_allows(self) -> None:
        self.assertFalse(self._denied(
            self._decide(self.CMD, labels={hook.LABEL}, repo=None)))

    def test_unreadable_labels_fail_open(self) -> None:
        self.assertFalse(self._denied(self._decide(self.CMD, labels=None)))

    def test_abandoned_label_allows(self) -> None:
        self.assertFalse(self._denied(self._decide(
            self.CMD, labels={hook.LABEL, hook.ABANDONED_LABEL})))

    def test_docs_naming_the_label_do_not_grant_it(self) -> None:
        """The whole point of the reforge: prose cannot be a credential."""
        doc = f"Apply {hook.EXECUTED_LABEL} after running it."
        self.assertTrue(self._denied(self._decide(
            f"gh issue close 849 -R {REPO} --comment '{doc}'",
            labels={hook.LABEL})))

    def test_bare_close_is_gated_once_repo_resolves(self) -> None:
        """The most common form must NOT be fail-open any more."""
        self.assertTrue(self._denied(
            self._decide("gh issue close 849", labels={hook.LABEL})))


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



class ReviewRegressionTests(unittest.TestCase):
    """Every defect the two independent reviews found, as a test."""

    def test_semicolon_inside_quoted_comment_still_parses(self) -> None:
        """Second review: private regex segmenter split inside quotes."""
        self.assertEqual(
            hook.find_close_targets(
                f'gh issue close 849 -R {REPO} --comment "done; verified"'),
            [(REPO, "849")])

    def test_pipe_inside_quoted_comment_still_parses(self) -> None:
        self.assertEqual(
            hook.find_close_targets(
                f'gh issue close 849 -R {REPO} --comment "a | b"'),
            [(REPO, "849")])

    def test_issue_url_form(self) -> None:
        self.assertEqual(
            hook.find_close_targets(
                "gh issue close https://github.com/vitalharmony/hrse/issues/849"),
            [(REPO, "849")])

    def test_env_prefixed_invocation(self) -> None:
        self.assertEqual(
            hook.find_close_targets(f"env gh issue close 849 -R {REPO}"),
            [(REPO, "849")])

    def test_var_assignment_prefixed_invocation(self) -> None:
        self.assertEqual(
            hook.find_close_targets(f"GH_TOKEN=x gh issue close 849 -R {REPO}"),
            [(REPO, "849")])

    def test_main_threads_payload_cwd_into_repo_resolution(self) -> None:
        """A prior round resolved against the hook's own cwd, not the
        command's, and checked the wrong repository's issue #N. Nothing
        else in this suite guards that fix."""
        seen: dict[str, str | None] = {}

        def spy(explicit: str | None, cwd: str | None = None) -> str | None:
            seen["cwd"] = cwd
            return REPO

        payload = {"tool_name": "Bash", "cwd": "/some/where",
                   "tool_input": {"command": "gh issue close 849"}}
        with mock.patch.object(hook, "resolve_repo", side_effect=spy), \
             mock.patch.object(hook, "labels_for", return_value=set()), \
             mock.patch("sys.stdin", mock.MagicMock()), \
             mock.patch("json.load", return_value=payload), \
             mock.patch("builtins.print"):
            hook.main()
        self.assertEqual(seen["cwd"], "/some/where")

    @staticmethod
    def _run(command: str, labels: set[str] | None) -> dict:
        with mock.patch.object(hook, "resolve_repo", return_value=REPO), \
             mock.patch.object(hook, "labels_for", return_value=labels), \
             mock.patch("sys.stdin", mock.MagicMock()), \
             mock.patch("json.load", return_value=bash(command)), \
             mock.patch("builtins.print") as printed:
            hook.main()
        return json.loads(printed.call_args[0][0])

    @staticmethod
    def _is_deny(payload: dict) -> bool:
        return (payload.get("hookSpecificOutput") or {}).get(
            "permissionDecision") == "deny"

    def test_label_matching_is_exact_not_substring(self) -> None:
        cmd = f"gh issue close 849 -R {REPO}"
        for near in ("not-migration-executed", "migration-executed-soon"):
            with self.subTest(label=near):
                self.assertTrue(self._is_deny(
                    self._run(cmd, {hook.LABEL, near})))

    def test_executed_label_allows(self) -> None:
        self.assertFalse(self._is_deny(self._run(
            f"gh issue close 849 -R {REPO}", {hook.LABEL, hook.EXECUTED_LABEL})))

    def test_unreadable_target_does_not_suppress_a_later_deny(self) -> None:
        """continue, not return: the second target must still be judged."""
        calls = iter([None, {hook.LABEL}])
        with mock.patch.object(hook, "resolve_repo", return_value=REPO), \
             mock.patch.object(hook, "labels_for", side_effect=lambda *_: next(calls)), \
             mock.patch("sys.stdin", mock.MagicMock()), \
             mock.patch("json.load", return_value=bash(
                 f"gh issue close 111 -R {REPO}; gh issue close 849 -R {REPO}")), \
             mock.patch("builtins.print") as printed:
            hook.main()
        self.assertTrue(self._is_deny(json.loads(printed.call_args[0][0])))

    def test_labels_for_returns_none_on_failure(self) -> None:
        with mock.patch.object(hook, "_gh", return_value=None):
            self.assertIsNone(hook.labels_for("o/r", "849"))

    def test_labels_for_parses_names(self) -> None:
        with mock.patch.object(hook, "_gh", return_value="tech-debt\ndata-migration\n"):
            self.assertEqual(hook.labels_for("o/r", "849"),
                             {"tech-debt", "data-migration"})


if __name__ == "__main__":
    unittest.main()
