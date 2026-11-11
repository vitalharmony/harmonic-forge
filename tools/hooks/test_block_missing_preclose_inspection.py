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
import os
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

    def test_gh_as_wrapper_is_gated(self) -> None:
        self.assert_target(
            f"gh-as vitalharmony gh api -X PATCH repos/{REPO}/issues/1476 -f state=closed",
            (REPO, "1476", "issue"))

    def test_wrapped_bash_login_shell_is_gated(self) -> None:
        self.assert_target(
            f"/bin/bash -lc 'gh-as vitalharmony gh api -X PATCH repos/{REPO}/issues/1476 -f state=closed'",
            (REPO, "1476", "issue"))

    def test_compound_wrapped_bash_login_shell_is_gated(self) -> None:
        self.assert_target(
            f"/bin/bash -lc 'echo ok && gh-as vitalharmony gh api -X PATCH repos/{REPO}/issues/1476 -f state=closed'",
            (REPO, "1476", "issue"))

    def test_compound_shell_with_launcher_prefixes_is_gated(self) -> None:
        payload = f"echo ok && gh-as vitalharmony gh api -X PATCH repos/{REPO}/issues/1476 -f state=closed"
        for prefix in ("env /bin/bash -lc", "command /bin/bash -lc", "nohup /bin/bash -lc"):
            self.assert_target(f"{prefix} '{payload}'", (REPO, "1476", "issue"))

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

    def test_pr_merge_without_a_number_is_a_target_with_no_number(self) -> None:
        """Bare `gh pr merge` targets the current branch's PR; resolving
        that from the payload is a guess, so it is returned with an empty
        number for main() to deny (harmonic-forge#1077 pass 2; this failed
        open before)."""
        self.assertEqual(hook.find_gated_targets("gh pr merge --squash"), [(None, "", "pr")])

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


class PrShaBindingTests(unittest.TestCase):
    """harmonic-forge#778 AC3: a merge with `preclose-inspected` still needs
    a completed receipt naming the PR's CURRENT head as reviewed. The label
    alone says a review happened once; it says nothing about which diff --
    new commits landing after the label was added is exactly harmonic-forge
    #774's failure mode."""

    # harmonic-forge#1077 AC5: a gated merge binds to the accepted head.
    CMD = f"gh pr merge 1486 -R {REPO} --squash --match-head-commit abc123"

    def _decide(self, head_sha: str | None, receipt_ok: bool,
                labels: set[str] | None = None, command: str | None = None) -> dict:
        if labels is None:
            labels = {hook.TOOLING_EXCEPTION_LABEL, hook.PRECLOSE_LABEL}
        with mock.patch.object(hook, "resolve_repo", return_value=REPO), \
             mock.patch.object(hook, "labels_for", return_value=labels), \
             mock.patch.object(hook, "issues_closed_by_pr", return_value=["1476"]), \
             mock.patch.object(hook, "_pr_head_sha", return_value=head_sha), \
             mock.patch.object(hook, "_preclose_receipt_ok", return_value=receipt_ok), \
             mock.patch("sys.stdin", mock.MagicMock()), \
             mock.patch("json.load", return_value=bash(command or self.CMD)), \
             mock.patch("builtins.print") as printed:
            hook.main()
        return json.loads(printed.call_args[0][0])

    def _denied(self, payload: dict) -> bool:
        return (payload.get("hookSpecificOutput") or {}).get(
            "permissionDecision") == "deny"

    def test_labelled_and_receipt_matches_head_allows(self):
        self.assertFalse(self._denied(self._decide("abc123", True)))

    def _reason(self, payload: dict) -> str:
        return (payload.get("hookSpecificOutput") or {}).get("permissionDecisionReason", "")

    def test_merge_without_match_head_commit_denies_with_the_exact_command(self):
        """harmonic-forge#1077 AC5."""
        payload = self._decide("abc123", True, command=f"gh pr merge 1486 -R {REPO} --squash")
        self.assertTrue(self._denied(payload))
        self.assertIn(f"gh pr merge 1486 --repo {REPO} --squash --match-head-commit abc123",
                      self._reason(payload))

    def test_merge_bound_to_another_head_denies(self):
        payload = self._decide(
            "abc123", True,
            command=f"gh pr merge 1486 -R {REPO} --squash --match-head-commit def456")
        self.assertTrue(self._denied(payload))

    def test_match_head_commit_forms_that_bind_allow(self):
        for command in (f"gh pr merge 1486 -R {REPO} --match-head-commit=abc123",
                        f"gh pr merge --match-head-commit ABC123 1486 -R {REPO}",
                        f"bash -c 'gh pr merge 1486 -R {REPO} --match-head-commit abc123'",
                        f"gh pr merge 1486 -R {REPO} --squash -b 'see --match-head-commit' "
                        f"--match-head-commit abc123 -d"):
            with self.subTest(command=command):
                self.assertFalse(self._denied(self._decide("abc123", True, command=command)))

    def test_rest_merge_is_never_bound(self):
        """Pass-2 sticky-wicket: with `--input`, gh moves `-f sha=` to the
        query string, so the REST form is denied for a gated PR whatever
        `sha=` it carries."""
        for command in (f"gh api -X PUT repos/{REPO}/pulls/1486/merge -f sha=abc123 "
                        f"--input body.json",
                        f"gh api -X PUT repos/{REPO}/pulls/1486/merge -f sha=abc123",
                        f"gh api -X PUT repos/{REPO}/pulls/1486/merge --field=sha=abc123",
                        f"gh api --method PUT repos/{REPO}/pulls/1486/merge -fsha=abc123"):
            with self.subTest(command=command):
                self.assertTrue(self._denied(self._decide("abc123", True, command=command)))

    def test_merge_without_a_pr_number_denies(self):
        """Pass 2: a bare merge picks the current branch's PR, which the hook
        cannot bind, so it denies and asks for the PR number."""
        payload = self._decide("abc123", True, command=f"gh pr merge -R {REPO} --squash")
        self.assertTrue(self._denied(payload))
        self.assertIn("gh pr merge <N>", self._reason(payload))

    def test_match_head_commit_inside_another_flags_value_denies(self):
        """Preclose pass 1: a flag-shaped VALUE never binds the merge."""
        for command in (
                f"gh pr merge 1486 -R {REPO} --squash --body --match-head-commit=abc123",
                f"gh pr merge 1486 -R {REPO} --squash -b --match-head-commit=abc123",
                f"gh pr merge 1486 -R {REPO} --squash --subject --match-head-commit abc123",
                f"gh pr merge 1486 -R {REPO} -t --match-head-commit=abc123",
                f"gh pr merge 1486 -R {REPO} --body-file --match-head-commit=abc123",
                f"gh pr merge 1486 -R {REPO} --body='x --match-head-commit=abc123'",
                f"gh pr merge 1486 -R {REPO} --squash -- --match-head-commit=abc123",
                f"gh pr merge 1486 -R {REPO} --squash --unknown --match-head-commit=abc123",
                f"gh api -X PUT repos/{REPO}/pulls/1486/merge -f commit_message=-fsha=abc123",
                f"gh api -X PUT repos/{REPO}/pulls/1486/merge -H -f -H sha=abc123"):
            with self.subTest(command=command):
                self.assertTrue(self._denied(self._decide("abc123", True, command=command)))

    def test_rest_merge_without_sha_denies(self):
        self.assertTrue(self._denied(self._decide(
            "abc123", True, command=f"gh api -X PUT repos/{REPO}/pulls/1486/merge")))

    def test_two_merges_of_the_pr_one_unbound_denies(self):
        self.assertTrue(self._denied(self._decide(
            "abc123", True,
            command=(f"gh pr merge 1486 -R {REPO} --match-head-commit abc123; "
                     f"gh pr merge 1486 -R {REPO}"))))

    def test_labelled_but_receipt_stale_denies(self):
        """A receipt exists but names an OLDER sha, or none exists at all --
        `_preclose_receipt_ok` returning False covers both."""
        self.assertTrue(self._denied(self._decide("abc123", False)))

    def test_head_sha_unresolvable_fails_closed(self):
        """Unlike a `_gh` read used only to DECIDE whether to check further
        (which fails open elsewhere in this hook), this point is reached only
        once the issue is already confirmed tooling-exception AND
        preclose-inspected labelled -- a SHA-bound receipt is required, and
        "cannot determine the head SHA" is indistinguishable from "not
        reviewed" for that purpose. Preclose finding: this previously fell
        through to allow, which meant an unresolvable head SHA authorized a
        merge with zero verification instead of blocking it."""
        self.assertTrue(self._denied(self._decide(None, True)))
        self.assertTrue(self._denied(self._decide(None, False)))

    def test_stale_receipt_denial_names_preclose_check_and_the_head_sha(self):
        payload = self._decide("abc123def456", False)
        reason = payload["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("preclose_check.py", reason)
        self.assertIn("abc123def456"[:12], reason)

    def test_stale_receipt_denial_names_the_pr_and_issue_too(self):
        payload = self._decide("abc123", False)
        reason = payload["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("1486", reason)
        self.assertIn("1476", reason)

    def test_without_the_preclose_label_the_older_denial_fires_first(self):
        """The label check still comes first -- an unlabelled PR gets the
        original hrse#1487 message, not a SHA-binding message with nothing
        to bind (no receipt is even looked for)."""
        with mock.patch.object(hook, "_pr_head_sha") as head_sha_mock:
            payload = self._decide(
                "abc123", True, labels={hook.TOOLING_EXCEPTION_LABEL})
        head_sha_mock.assert_not_called()
        self.assertTrue(self._denied(payload))

    def test_bare_issue_close_is_unaffected_by_sha_binding(self):
        """AC5: `block_closing_keywords.py` is unchanged, and this hook's own
        close path stays label-only too -- no SHA exists for a bare close."""
        with mock.patch.object(hook, "resolve_repo", return_value=REPO), \
             mock.patch.object(hook, "labels_for", return_value={
                 hook.TOOLING_EXCEPTION_LABEL, hook.PRECLOSE_LABEL}), \
             mock.patch.object(hook, "_pr_head_sha") as head_sha_mock, \
             mock.patch("sys.stdin", mock.MagicMock()), \
             mock.patch("json.load",
                       return_value=bash(f"gh issue close 1476 --repo {REPO}")), \
             mock.patch("builtins.print") as printed:
            hook.main()
        head_sha_mock.assert_not_called()
        self.assertFalse(self._denied(json.loads(printed.call_args[0][0])))


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


class ApiMergeFormTests(unittest.TestCase):
    """harmonic-forge#549 AC4 — the REST merge form is gated too.

    This hook parsed three shapes: `gh issue close`, `gh pr merge`, and
    `gh api PATCH ... state=closed`. `gh api -X PUT .../pulls/N/merge` matched
    none of them and passed unexamined, so a Tooling-Exception issue without
    `preclose-inspected` could be merged through the REST form with nothing
    objecting. `batch_auth.classify_pr_merge` has always handled both forms,
    which is why BATCH stopped a live PUT merge on 2026-09-09 while this guard
    did not look at it — two hooks parsing one command class to different
    depths.
    """

    def test_put_merge_is_recognized(self):
        self.assertEqual(
            hook.find_gated_targets(
                "gh api -X PUT repos/o/r/pulls/7/merge -f merge_method=squash"),
            [("o/r", "7", "pr")])

    def test_method_long_flag_is_recognized(self):
        self.assertEqual(
            hook.find_gated_targets("gh api --method PUT repos/o/r/pulls/7/merge"),
            [("o/r", "7", "pr")])

    def test_attached_flag_form_is_recognized(self):
        self.assertEqual(
            hook.find_gated_targets("gh api -XPUT repos/o/r/pulls/8/merge"),
            [("o/r", "8", "pr")])

    def test_a_non_put_request_to_the_merge_path_is_not_a_merge(self):
        """GET .../merge asks whether a PR is merged; it changes nothing."""
        self.assertEqual(
            hook.find_gated_targets("gh api -X GET repos/o/r/pulls/7/merge"), [])

    def test_put_elsewhere_is_not_a_merge(self):
        self.assertEqual(
            hook.find_gated_targets("gh api -X PUT repos/o/r/pulls/7"), [])

    def test_cli_and_rest_forms_agree(self):
        """The whole point: both spellings of one action reach one verdict."""
        cli = hook.find_gated_targets("gh pr merge 7 --repo o/r --squash")
        rest = hook.find_gated_targets("gh api -X PUT repos/o/r/pulls/7/merge")
        self.assertEqual(cli, rest)

    def test_the_pattern_is_batch_auths_not_a_second_copy(self):
        """A second copy of the regex is what drifts, so assert the identity."""
        import batch_auth

        self.assertIsNotNone(
            batch_auth.API_MERGE_PATH.search("repos/o/r/pulls/7/merge"))
        self.assertEqual(
            batch_auth.classify_pr_merge(
                ["gh", "api", "-X", "PUT", "repos/o/r/pulls/7/merge"]),
            ("o/r", 7))


class PreCloseReceiptOkIntegrationTests(unittest.TestCase):
    """`_preclose_receipt_ok` against the REAL `preclose_check.find_receipt`
    -- `PrShaBindingTests` above mocks it out entirely, which proves the
    hook's own branching but not that the two modules actually agree on a
    receipt's shape. Isolated to a temp HOME, same reasoning as
    `test_preclose_check.py`'s `ScratchRepo` (harmonic-forge#778)."""

    def setUp(self):
        import tempfile
        self.home_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.home_tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"HOME": self.home_tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gh"))
        import preclose_check
        self.preclose_check = preclose_check

    def _write_receipt(self, repo, issue, sha, status="complete"):
        directory = self.preclose_check.receipt_dir()
        directory.mkdir(parents=True, exist_ok=True)
        self.preclose_check.receipt_path(repo, issue).write_text(json.dumps({
            "repo": repo, "issue": issue, "reviewed_sha": sha,
            "refuters": 3, "status": status,
        }))

    def test_matching_complete_receipt_is_ok(self):
        self._write_receipt(REPO, 1476, "deadbeef")
        self.assertTrue(hook._preclose_receipt_ok(REPO, "1476", "deadbeef"))

    def test_receipt_for_a_different_sha_is_not_ok(self):
        self._write_receipt(REPO, 1476, "old-sha")
        self.assertFalse(hook._preclose_receipt_ok(REPO, "1476", "new-sha"))

    def test_no_receipt_at_all_is_not_ok(self):
        self.assertFalse(hook._preclose_receipt_ok(REPO, "1476", "deadbeef"))

    def test_incomplete_status_is_not_ok(self):
        self._write_receipt(REPO, 1476, "deadbeef", status="planned")
        self.assertFalse(hook._preclose_receipt_ok(REPO, "1476", "deadbeef"))

    def _write_history(self, issue, entries):
        directory = self.preclose_check.receipt_dir()
        directory.mkdir(parents=True, exist_ok=True)
        self.preclose_check.receipt_path(REPO, issue).write_text(json.dumps({
            "repo": REPO, "issue": issue, "reviewed_sha": entries[-1]["sha"],
            "refuters": 5, "status": "complete", "pass_history": entries}))

    def test_blocked_covering_pass_is_not_ok(self):
        """harmonic-forge#1330 TC 1f: a hostile-author block refuses its own head."""
        self._write_history(1476, [{"sha": "deadbeef", "epoch": 0, "surviving": 1,
                                    "hostile_blocking": ["tools/x.py:1"]}])
        self.assertFalse(hook._preclose_receipt_ok(REPO, "1476", "deadbeef"))
        self._write_history(1476, [{"sha": "deadbeef", "epoch": 0, "surviving": 1,
                                    "hostile_blocking": []}])
        self.assertTrue(hook._preclose_receipt_ok(REPO, "1476", "deadbeef"))

    def test_a_later_clean_pass_clears_the_block(self):
        """harmonic-forge#1330 TC 1g: only the last pass of the current epoch blocks."""
        self._write_history(1476, [
            {"sha": "old", "epoch": 0, "surviving": 1, "hostile_blocking": ["tools/x.py:1"]},
            {"sha": "new", "epoch": 0, "surviving": 0, "hostile_blocking": []}])
        self.assertTrue(hook._preclose_receipt_ok(REPO, "1476", "new"))
        self._write_history(1476, [
            {"sha": "old", "epoch": 0, "surviving": 1, "hostile_blocking": ["tools/x.py:1"]},
            {"sha": "new", "epoch": 1, "surviving": 0, "hostile_blocking": []}])
        self.assertTrue(hook._preclose_receipt_ok(REPO, "1476", "new"))

    def test_stale_message_names_the_hostile_block(self):
        """harmonic-forge#1330 TC 1h: the deny names the block, never the cache
        note, and never offers to run another pass."""
        self._write_history(1476, [{
            "sha": "deadbeef", "epoch": 0, "surviving": 1,
            "change_identity": {"head_sha": "deadbeef", "merge_base": "b", "tree": "t"},
            "hostile_blocking": ["tools/x.py:1"]}])
        message = hook._stale_receipt_message(REPO, "1476", "9", "deadbeef")
        self.assertIn("hostile-author", message)
        self.assertIn("tools/x.py:1", message)
        self.assertNotIn("Acceptance replays", message)
        self.assertNotIn("Run the pre-close pass", message)
        reason = hook._stale_receipt_reason(REPO, "1476", "9", "deadbeef")
        self.assertIn("tools/x.py:1", reason)
        self.assertNotIn("Run the pre-close pass", reason)

    def test_block_at_the_pass_cap_keeps_the_sticky_wicket_route(self):
        """harmonic-forge#1330 pass 1 survivor 1: at MAX_PASSES the cap text
        leads, and the block is still named after it."""
        self._write_history(1476, [
            {"sha": "aaaa", "epoch": 0, "surviving": 2, "hostile_blocking": []},
            {"sha": "bbbb", "epoch": 0, "surviving": 1, "hostile_blocking": ["tools/x.py:1"]}])
        for text in (hook._stale_receipt_message(REPO, "1476", "9", "bbbb"),
                     hook._stale_receipt_reason(REPO, "1476", "9", "bbbb")):
            self.assertIn("sticky-wicket", text)
            self.assertIn("tools/x.py:1", text)
            self.assertNotIn("Run the pre-close pass", text)
            self.assertLess(text.index("sticky-wicket"), text.index("tools/x.py:1"))

    def test_block_on_a_moved_head_still_says_to_run_a_pass(self):
        """harmonic-forge#1330 pass 1 survivor 1: a head the blocked pass does
        not cover is told to run a pass, with the block named after it."""
        self._write_history(1476, [{"sha": "deadbeef", "epoch": 0, "surviving": 1,
                                    "hostile_blocking": ["tools/x.py:1"]}])
        for text in (hook._stale_receipt_message(REPO, "1476", "9", "fixhead"),
                     hook._stale_receipt_reason(REPO, "1476", "9", "fixhead")):
            self.assertIn("Run the pre-close pass", text)
            self.assertIn("tools/x.py:1", text)
            self.assertLess(text.index("Run the pre-close pass"), text.index("tools/x.py:1"))

    # harmonic-forge#1330 sticky-wicket PATCH: one test per branch of the
    # single composer, `_deny_text`. Each asserts the composer and both
    # public wrappers agree.
    BLOCK = "tools/x.py:1"
    IDENT = {"head_sha": "deadbeef", "merge_base_sha": "b", "tree": "t"}

    def _texts(self, head: str) -> list[str]:
        return [hook._deny_text(REPO, "1476", "9", head),
                hook._stale_receipt_message(REPO, "1476", "9", head),
                hook._stale_receipt_reason(REPO, "1476", "9", head)]

    def _receipt_now(self):
        return self.preclose_check.find_receipt(REPO, 1476)

    def _write_cluster(self, entries):
        self._write_history(1476, entries)
        path = self.preclose_check.receipt_path(REPO, 1476)
        data = json.loads(path.read_text())
        data["mechanism_cluster"] = {"pass_sha": entries[-1]["sha"], "epoch": 0,
                                     "mechanisms": ["re-parsing shell text"],
                                     "verdict": None, "comment_url": None}
        path.write_text(json.dumps(data))

    def test_deny_text_a_covered_block_is_the_block_alone(self):
        """(a) Covered head, one pass, no cluster, blocked: exactly close_refusal."""
        import preclose_passes
        self._write_history(1476, [{"sha": "deadbeef", "epoch": 0, "surviving": 1,
                                    "change_identity": self.IDENT,
                                    "hostile_blocking": [self.BLOCK]}])
        expected = preclose_passes.close_refusal(self._receipt_now())
        for text in self._texts("deadbeef"):
            self.assertEqual(text, expected)
            self.assertNotIn("Acceptance replays", text)

    def test_deny_text_an_open_cluster_leads_the_block(self):
        """(b) Open cluster plus block: the cluster route first, then the block."""
        import preclose_passes
        self._write_cluster([{"sha": "deadbeef", "epoch": 0, "surviving": 2,
                              "change_identity": self.IDENT,
                              "hostile_blocking": [self.BLOCK]}])
        route = preclose_passes.CLUSTER_ROUTE.format(
            mechanisms="re-parsing shell text", repo=REPO, issue="1476")
        for text in self._texts("deadbeef"):
            self.assertTrue(text.startswith(route), text[:80])
            self.assertIn(self.BLOCK, text[len(route):])

    def test_open_cluster_at_the_reforge_cap_never_offers_reforge(self):
        """harmonic-forge#1330 post-verdict check: an open cluster in epoch 2
        routes to sticky-wicket without offering the --force --reforge the
        cap would refuse, in refusal() and in the hook deny."""
        import preclose_passes
        self._write_history(1476, [{"sha": "deadbeef", "epoch": 2, "surviving": 2,
                                    "hostile_blocking": []}])
        path = self.preclose_check.receipt_path(REPO, 1476)
        data = json.loads(path.read_text())
        data["mechanism_cluster"] = {"pass_sha": "deadbeef", "epoch": 2,
                                     "mechanisms": ["re-parsing shell text"],
                                     "verdict": None, "comment_url": None}
        path.write_text(json.dumps(data))
        receipt = self._receipt_now()
        refused = preclose_passes.refusal(receipt, "cccc", repo=REPO, issue=1476)
        for text in [refused, *self._texts("cccc")]:
            self.assertIn("sticky-wicket", text)
            self.assertIn(preclose_passes.CLUSTER_ROUTE_REFORGE_AT_CAP, text)
            self.assertNotIn(preclose_passes.CLUSTER_ROUTE_REFORGE, text)

    def test_deny_text_the_pass_cap_leads_the_block(self):
        """(c) Pass cap plus block: the cap text first, then the block."""
        self._write_history(1476, [
            {"sha": "aaaa", "epoch": 0, "surviving": 2, "hostile_blocking": []},
            {"sha": "bbbb", "epoch": 0, "surviving": 1, "hostile_blocking": [self.BLOCK]}])
        for text in self._texts("cccc"):
            self.assertTrue(text.startswith("Blocked: PR #9"), text[:80])
            self.assertLess(text.index("sticky-wicket"), text.index(self.BLOCK))
            self.assertNotIn("Run the pre-close pass", text)

    def test_deny_text_a_moved_head_runs_a_pass_then_names_the_block(self):
        """(d) Moved head plus block: the run-a-pass text, then the block."""
        self._write_history(1476, [{"sha": "deadbeef", "epoch": 0, "surviving": 1,
                                    "hostile_blocking": [self.BLOCK]}])
        for text in self._texts("fixhead"):
            self.assertTrue(text.startswith("Blocked: PR #9"), text[:80])
            self.assertLess(text.index("Run the pre-close pass"), text.index(self.BLOCK))

    def test_deny_text_the_cache_note_comes_after_the_route(self):
        """(e) Cap or cluster with a cache-note-eligible entry: the note is last."""
        cap = [{"sha": "aaaa", "epoch": 0, "surviving": 2, "hostile_blocking": []},
               {"sha": "deadbeef", "epoch": 0, "surviving": 1,
                "change_identity": self.IDENT, "hostile_blocking": [self.BLOCK]}]
        cluster = [{"sha": "deadbeef", "epoch": 0, "surviving": 2,
                    "change_identity": self.IDENT, "hostile_blocking": []}]
        for write, entries in ((self._write_history, cap), (self._write_cluster, cluster)):
            if write == self._write_history:
                write(1476, entries)
            else:
                write(entries)
            for text in self._texts("newhead"):
                with self.subTest(case=len(entries)):
                    self.assertIn("Acceptance replays", text)
                    self.assertLess(text.index("sticky-wicket"), text.index("Acceptance replays"))

    def test_route_text_at_both_caps_never_offers_a_new_branch(self):
        """(f) Epoch 2, both passes with survivors: refusal() and the hook
        deny carry the cap wording, never "'reforge' means a new branch",
        and cap_message still returns STICKY_WICKET for its three callers."""
        import preclose_passes
        entries = [{"sha": "aaaa", "epoch": 2, "surviving": 2, "hostile_blocking": []},
                   {"sha": "bbbb", "epoch": 2, "surviving": 1, "hostile_blocking": []}]
        self._write_history(1476, entries)
        receipt = self._receipt_now()
        refused = preclose_passes.refusal(receipt, "cccc", repo=REPO, issue=1476)
        for text in [refused, *self._texts("cccc")]:
            self.assertIn("re-scope decision", text)
            self.assertNotIn("'reforge' means a new branch", text)
        passes = preclose_passes.current(preclose_passes.history(receipt))
        self.assertIs(preclose_passes.cap_message(passes), preclose_passes.STICKY_WICKET)

    def _close(self, command: str) -> dict:
        with mock.patch.object(hook, "resolve_repo", return_value=REPO), \
             mock.patch.object(hook, "labels_for", return_value={
                 hook.TOOLING_EXCEPTION_LABEL, hook.PRECLOSE_LABEL}), \
             mock.patch("sys.stdin", mock.MagicMock()), \
             mock.patch("json.load", return_value=bash(command)), \
             mock.patch("builtins.print") as printed:
            hook.main()
        return json.loads(printed.call_args[0][0])

    def _assert_close_follows_the_block(self, command: str) -> None:
        self._write_history(1476, [{"sha": "deadbeef", "epoch": 0, "surviving": 1,
                                    "hostile_blocking": ["tools/x.py:1"]}])
        payload = self._close(command)
        output = payload.get("hookSpecificOutput") or {}
        self.assertEqual(output.get("permissionDecision"), "deny")
        self.assertIn("tools/x.py:1", output.get("permissionDecisionReason", ""))
        for entry in ({"sha": "deadbeef", "epoch": 0, "surviving": 1, "hostile_blocking": []},
                      {"sha": "deadbeef", "epoch": 0, "surviving": 1}):
            with self.subTest(entry=entry):
                self._write_history(1476, [entry])
                self.assertEqual(self._close(command), {})

    def test_bare_issue_close_is_refused_by_a_hostile_block(self):
        """harmonic-forge#1330 pass 1 survivor 2: `gh issue close` on a blocked
        receipt is denied; on a clean or legacy receipt it is allowed."""
        self._assert_close_follows_the_block(f"gh issue close 1476 --repo {REPO}")

    def test_api_close_is_refused_by_a_hostile_block(self):
        """harmonic-forge#1330 pass 1 survivor 2: the `gh api` close form too."""
        self._assert_close_follows_the_block(
            f"gh api -X PATCH repos/{REPO}/issues/1476 -f state=closed")

    def test_stale_head_with_unresolved_cluster_routes_before_offering_a_pass(self):
        directory = self.preclose_check.receipt_dir()
        directory.mkdir(parents=True, exist_ok=True)
        self.preclose_check.receipt_path(REPO, 1476).write_text(json.dumps({
            "repo": REPO, "issue": 1476, "reviewed_sha": "old", "refuters": 5,
            "status": "complete", "pass_history": [{
                "sha": "old", "patch_id": "p", "surviving": 2,
                "mechanisms": ["re-parsing shell text"], "epoch": 0,
            }],
            "mechanism_cluster": {"pass_sha": "old", "epoch": 0,
                                  "mechanisms": ["re-parsing shell text"],
                                  "verdict": None, "comment_url": None},
        }))
        message = hook._stale_receipt_message(REPO, "1476", "9", "new-head")
        self.assertIn("sticky-wicket", message)
        self.assertNotIn("Run the pre-close pass", message)


if __name__ == "__main__":
    unittest.main()
