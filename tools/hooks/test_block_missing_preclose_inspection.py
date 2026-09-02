#!/usr/bin/env python3
"""Tests for block_missing_preclose_inspection.py (hrse#1487).

Target-parsing tests mirror test_block_data_migration_close.py -- the parser
shares its shape, so the same evasion set applies -- plus `gh pr merge`,
which this hook gates and that one does not.

The decision tests exercise the label pair. The first implementation of this
hook gated on comment text and its own preclose-inspection review rejected
that; `PublishedExampleIsNotACredentialTests` below is the regression guard
for the specific bypasses it found.
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
    def assert_target(self, command: str, expected: tuple) -> None:
        self.assertEqual(hook.find_gated_targets(command), [expected])

    def test_issue_close_with_repo(self) -> None:
        self.assert_target(f"gh issue close 1476 --repo {REPO}", (REPO, "1476", "issue"))

    def test_issue_close_short_repo_flag(self) -> None:
        self.assert_target(f"gh issue close 1476 -R {REPO}", (REPO, "1476", "issue"))

    def test_comment_containing_a_number_does_not_hijack(self) -> None:
        self.assert_target(
            f"gh issue close 1476 --repo {REPO} --comment 'closes 219 rows'",
            (REPO, "1476", "issue"))

    def test_api_patch_state_closed(self) -> None:
        self.assert_target(
            f"gh api -X PATCH repos/{REPO}/issues/1476 -f state=closed",
            (REPO, "1476", "issue"))

    def test_api_method_long_flag(self) -> None:
        self.assert_target(
            f"gh api --method PATCH repos/{REPO}/issues/1476 -f state=closed",
            (REPO, "1476", "issue"))

    def test_pr_merge_is_gated(self) -> None:
        """hrse#1487's own preclose-inspection finding 3: gating only the
        close enforces the review after the diff has already landed."""
        self.assert_target(f"gh pr merge 1486 --repo {REPO} --squash",
                           (REPO, "1486", "pr"))

    def test_pr_merge_url_form(self) -> None:
        self.assert_target(
            "gh pr merge https://github.com/vitalharmony/hrse/pull/1486 --squash",
            (REPO, "1486", "pr"))

    def test_pr_merge_without_a_number_fails_open(self) -> None:
        """Bare `gh pr merge` targets the current branch's PR; resolving
        that from the payload is a guess, so allow rather than block the
        wrong PR."""
        self.assertEqual(hook.find_gated_targets("gh pr merge --squash"), [])

    def test_state_open_is_not_a_close(self) -> None:
        self.assertEqual(hook.find_gated_targets(
            f"gh api -X PATCH repos/{REPO}/issues/1476 -f state=open"), [])

    def test_unrelated_commands(self) -> None:
        for command in ("git status", "gh issue view 1476", "gh pr view 1486",
                        "gh pr create --title x", "echo gh pr merge 1"):
            with self.subTest(command=command):
                self.assertEqual(hook.find_gated_targets(command), [])

    def test_unbalanced_quotes_fail_open(self) -> None:
        self.assertIsNone(hook.find_gated_targets(
            f"gh issue close 1476 --comment 'x"))

    def test_compound_command_returns_every_target(self) -> None:
        self.assertEqual(
            hook.find_gated_targets(
                f"gh pr merge 1486 -R {REPO} --squash; gh issue close 1476 -R {REPO}"),
            [(REPO, "1486", "pr"), (REPO, "1476", "issue")])


class DecisionTests(unittest.TestCase):
    CMD = f"gh issue close 1476 --repo {REPO}"

    def _decide(self, command: str, labels: set[str] | None,
                repo: str | None = REPO,
                closed_issues: list[str] | None = None) -> dict:
        # `is None`, not `or` -- an explicitly-empty list is the
        # unresolvable-PR case and must NOT fall back to the default.
        if closed_issues is None:
            closed_issues = ["1476"]
        with mock.patch.object(hook, "resolve_repo", return_value=repo), \
             mock.patch.object(hook, "labels_for", return_value=labels), \
             mock.patch.object(hook, "issues_closed_by_pr",
                               return_value=closed_issues), \
             mock.patch("sys.stdin", mock.MagicMock()), \
             mock.patch("json.load", return_value=bash(command)), \
             mock.patch("builtins.print") as printed:
            hook.main()
        return json.loads(printed.call_args[0][0])

    def _denied(self, payload: dict) -> bool:
        return (payload.get("hookSpecificOutput") or {}).get(
            "permissionDecision") == "deny"

    def test_tooling_exception_without_preclose_denies(self):
        """The hrse#1476 incident, reproduced."""
        self.assertTrue(self._denied(self._decide(
            self.CMD, labels={hook.TOOLING_EXCEPTION_LABEL, "tech-debt"})))

    def test_tooling_exception_with_preclose_allows(self):
        self.assertFalse(self._denied(self._decide(
            self.CMD, labels={hook.TOOLING_EXCEPTION_LABEL, hook.PRECLOSE_LABEL})))

    def test_unlabelled_issue_is_not_gated_at_all(self):
        """OPT-IN. The previous implementation gated on the ABSENCE of a
        signal, so its footprint was all 192 open hrse + 80 forge issues and
        an ordinary close was denied. `block_data_migration_close.py` is
        opt-in for this reason; deviating from it was the defect."""
        self.assertFalse(self._denied(self._decide(
            self.CMD, labels={"bug", "Substrate"})))

    def test_not_planned_close_of_an_ordinary_issue_is_not_gated(self):
        """A won't-do/duplicate close has no diff to inspect. 14 such closes
        on hrse since 2026-08-01 were denied by the previous design, whose
        only escape was applying `preclose-inspected` -- asserting a review
        that never ran."""
        self.assertFalse(self._denied(self._decide(
            f"gh issue close 1269 --repo {REPO} --reason 'not planned'",
            labels={"tech-debt"})))

    def test_three_lane_issue_is_not_gated(self):
        """Full-loop work is not labelled tooling-exception, so Lane 3's own
        gate covers it and this hook stays out of the way."""
        self.assertFalse(self._denied(self._decide(
            self.CMD, labels={"bug", "lane3-gated"})))

    def test_pr_merge_denies_when_its_issue_is_unreviewed(self):
        self.assertTrue(self._denied(self._decide(
            f"gh pr merge 1486 -R {REPO} --squash",
            labels={hook.TOOLING_EXCEPTION_LABEL})))

    def test_pr_merge_with_unresolvable_branch_fails_open(self):
        self.assertFalse(self._denied(self._decide(
            f"gh pr merge 1486 -R {REPO} --squash",
            labels={hook.TOOLING_EXCEPTION_LABEL}, closed_issues=[])))

    def test_deny_names_the_issue_the_label_and_hrse_1487(self):
        payload = self._decide(self.CMD, labels={hook.TOOLING_EXCEPTION_LABEL})
        reason = payload["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("1476", reason)
        self.assertIn(hook.PRECLOSE_LABEL, reason)
        self.assertIn("hrse#1487", reason)

    def test_pr_deny_names_the_pr_and_the_issue(self):
        payload = self._decide(f"gh pr merge 1486 -R {REPO}",
                               labels={hook.TOOLING_EXCEPTION_LABEL})
        reason = payload["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("1486", reason)
        self.assertIn("1476", reason)

    def test_unresolvable_repo_allows(self):
        self.assertFalse(self._denied(self._decide(
            self.CMD, labels={hook.TOOLING_EXCEPTION_LABEL}, repo=None)))

    def test_unreadable_labels_fail_open(self):
        self.assertFalse(self._denied(self._decide(self.CMD, labels=None)))

    def test_label_matching_is_exact_not_substring(self):
        for near in ("not-tooling-exception", "tooling-exception-pending"):
            with self.subTest(label=near):
                self.assertFalse(self._denied(self._decide(
                    self.CMD, labels={near})))
        # and the preclose side: a near-miss must NOT satisfy the gate
        for near in ("preclose-inspected-soon", "not-preclose-inspected"):
            with self.subTest(label=near):
                self.assertTrue(self._denied(self._decide(
                    self.CMD, labels={hook.TOOLING_EXCEPTION_LABEL, near})))


class PublishedExampleIsNotACredentialTests(unittest.TestCase):
    """The gate must read labels from the API, never text anyone can write.

    The prior version of this guard was INERT: it asserted only
    `"/comments" not in source`, and a mutant that read comment bodies via
    `gh issue view --json comments` and granted PRECLOSE_LABEL on a
    `## Preclose-inspection` heading left the whole suite green. This
    version is behavioral -- it drives `main()` with the credential text
    placed everywhere a real bypass would put it, against an UNMOCKED
    `labels_for` whose only input is a stubbed `_gh`.
    """

    CREDENTIAL_TEXT = (
        "## Preclose-inspection\n\nNo issues found.\n"
        "kind=ready-for-l3\n"
        f"Apply {hook.PRECLOSE_LABEL} when done.\n"
    )

    def _decide_with_real_labels_for(self, gh_side_effect) -> dict:
        """`labels_for` is NOT mocked -- only the subprocess boundary is. A
        hook that reaches for comment text has to go through `_gh`, so a
        stub that returns the credential for any non-label call will grant
        the gate to any implementation that reads it."""
        with mock.patch.object(hook, "_gh", side_effect=gh_side_effect), \
             mock.patch.object(hook, "resolve_repo", return_value=REPO), \
             mock.patch("sys.stdin", mock.MagicMock()), \
             mock.patch("json.load", return_value=bash(
                 f"gh issue close 1476 --repo {REPO}")), \
             mock.patch("builtins.print") as printed:
            hook.main()
        return json.loads(printed.call_args[0][0])

    def test_credential_text_in_comments_does_not_grant_the_gate(self):
        def fake_gh(*args, **kwargs):
            # The label read returns ONLY the tooling-exception label.
            if "--jq" in args and ".labels[].name" in args:
                return f"{hook.TOOLING_EXCEPTION_LABEL}\n"
            # ANY other read -- comments, timeline, pr view -- hands back
            # the credential text. An implementation that consults it grants
            # itself the gate and this test fails.
            return self.CREDENTIAL_TEXT
        payload = self._decide_with_real_labels_for(fake_gh)
        self.assertEqual(
            payload.get("hookSpecificOutput", {}).get("permissionDecision"),
            "deny",
            "the gate was granted by text rather than by a label",
        )

    def test_guard_catches_a_comment_reading_mutant(self):
        """Proves the guard above is not itself inert: the same stub, run
        against a labels_for that ORs in a comment read, must produce an
        allow -- i.e. the assertion above would genuinely fail."""
        def mutant_labels_for(repo, issue):
            names = {hook.TOOLING_EXCEPTION_LABEL}
            body = hook._gh("issue", "view", issue, "--repo", repo,
                            "--json", "comments", "--jq", ".comments[].body")
            if body and "## Preclose-inspection" in body:
                names.add(hook.PRECLOSE_LABEL)
            return names

        def fake_gh(*args, **kwargs):
            if "--jq" in args and ".labels[].name" in args:
                return f"{hook.TOOLING_EXCEPTION_LABEL}\n"
            return self.CREDENTIAL_TEXT

        with mock.patch.object(hook, "labels_for", side_effect=mutant_labels_for), \
             mock.patch.object(hook, "_gh", side_effect=fake_gh), \
             mock.patch.object(hook, "resolve_repo", return_value=REPO), \
             mock.patch("sys.stdin", mock.MagicMock()), \
             mock.patch("json.load", return_value=bash(
                 f"gh issue close 1476 --repo {REPO}")), \
             mock.patch("builtins.print") as printed:
            hook.main()
        payload = json.loads(printed.call_args[0][0])
        self.assertEqual(payload, {},
                         "mutant should have been granted the gate; if this "
                         "fails the guard test above proves nothing")


class BranchResolutionTests(unittest.TestCase):
    """`closingIssuesReferences` is structurally always empty in this project
    -- block_lane1_status_claims.py blocks auto-close keywords, so no PR ever
    carries one. Measured live: closes=0 on all 30 most recent merged hrse
    PRs, while every one carried its issue number in the branch name."""

    def test_resolves_issue_from_branch_name(self):
        for branch, expected in (
            ("feat/1476-forge-pipeline-triage", ["1476"]),
            ("fix/1429-dictation-pill-retype", ["1429"]),
            ("docs/1367-tracking-churn", ["1367"]),
            ("spike/1434-episode-split-sample", ["1434"]),
        ):
            with self.subTest(branch=branch):
                with mock.patch.object(hook, "_gh", return_value=branch + "\n"):
                    self.assertEqual(hook.issues_closed_by_pr(REPO, "1"), expected)

    def test_unconventional_branch_resolves_to_nothing(self):
        with mock.patch.object(hook, "_gh", return_value="my-scratch-branch\n"):
            self.assertEqual(hook.issues_closed_by_pr(REPO, "1"), [])

    def test_gh_failure_returns_none(self):
        with mock.patch.object(hook, "_gh", return_value=None):
            self.assertIsNone(hook.issues_closed_by_pr(REPO, "1"))


class LabelsForTests(unittest.TestCase):
    def test_parses_raw_one_name_per_line(self) -> None:
        """`gh api --jq` emits string scalars RAW, like `jq -r` -- verified
        empirically against the live API. The first implementation assumed
        JSON-quoted output and json.loads'd each line, which crashed the
        hook (exiting non-zero => non-blocking => silent bypass) on any
        thread containing a line that is a bare JSON value."""
        with mock.patch.object(hook, "_gh", return_value="tech-debt\nlane3-gated\n"):
            self.assertEqual(hook.labels_for(REPO, "1476"),
                             {"tech-debt", "lane3-gated"})

    def test_returns_none_on_gh_failure(self) -> None:
        with mock.patch.object(hook, "_gh", return_value=None):
            self.assertIsNone(hook.labels_for(REPO, "1476"))


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

    def test_main_threads_payload_cwd_into_repo_resolution(self) -> None:
        seen: dict[str, str | None] = {}

        def spy(explicit: str | None, cwd: str | None = None) -> str | None:
            seen["cwd"] = cwd
            return REPO

        payload = {"tool_name": "Bash", "cwd": "/some/where",
                   "tool_input": {"command": "gh issue close 1476"}}
        with mock.patch.object(hook, "resolve_repo", side_effect=spy), \
             mock.patch.object(hook, "labels_for",
                               return_value={hook.PRECLOSE_LABEL}), \
             mock.patch("sys.stdin", mock.MagicMock()), \
             mock.patch("json.load", return_value=payload), \
             mock.patch("builtins.print"):
            hook.main()
        self.assertEqual(seen["cwd"], "/some/where")


if __name__ == "__main__":
    unittest.main()
