#!/usr/bin/env python3
"""a private-repo incident — `ready-for-l3`/`ae` must refuse when no PR is open against
`main`.

Every consuming repo's CI workflow triggers only on `pull_request` or
`push`-to-`main` — a feature-branch push alone guarantees zero check runs on
the attested SHA, and Lane 3's own R-0354 then mechanically (and correctly)
returns BLOCKED. Confirmed live three times in one session (a private-repo incident,
a private-repo incident, a private-repo incident) before this closed the gap at its source: `ready-for-l3`
and `ae` must not be postable without an open PR already existing.
"""
import ast
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import l1_post as L  # noqa: E402

SOURCE = Path(__file__).resolve().parent / "l1_post.py"


def _pr_list_result(prs: list[dict]) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(("gh",), 0, json.dumps(prs), "")


def _repo_view_result(repo: str = "vitalharmony/hrse") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(("gh",), 0, repo + "\n", "")


def _git_remote_result(repo: str = "vitalharmony/hrse") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(("git",), 0, f"https://github.com/{repo}.git\n", "")


def _run_returning(pr_list: subprocess.CompletedProcess,
                    repo: str = "vitalharmony/hrse",
                    *, git_remote_fails: bool = False):
    """harmonic-forge#220: `require_open_pr` resolves the cwd's repo from
    `git remote get-url origin` (no API at all) and lists PRs over REST
    (`gh api repos/<repo>/pulls?...`), falling back to the original
    `gh repo view` + `gh pr list` GraphQL pair only when git cannot answer.
    Routes each call to its own canned response by argv shape.

    `git_remote_fails=True` forces the fallback path, so the GraphQL branch
    stays covered rather than becoming dead code nothing exercises."""
    def _run(*args, **kwargs):
        argv = args[0] if args and isinstance(args[0], (list, tuple)) else args
        if "remote" in argv:
            if git_remote_fails:
                return subprocess.CompletedProcess(("git",), 128, "", "not a git repository")
            return _git_remote_result(repo)
        if "view" in argv:
            return _repo_view_result(repo)
        return pr_list
    return _run


def _fn(name: str) -> ast.FunctionDef:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in l1_post.py")


class RequireOpenPr(unittest.TestCase):
    def test_no_pr_at_all_refuses(self) -> None:
        with mock.patch.object(L, "run", side_effect=_run_returning(_pr_list_result([]))):
            with self.assertRaises(SystemExit):
                L.require_open_pr("vitalharmony/hrse", "feat/1234-thing")

    def test_a_closed_pr_does_not_satisfy_the_check(self) -> None:
        """A merged-and-reopened-branch edge case: a CLOSED PR on record is
        not an OPEN one -- CI still never ran on THIS SHA's push."""
        with mock.patch.object(
            L, "run", side_effect=_run_returning(_pr_list_result([{"number": 1, "state": "CLOSED"}]))
        ):
            with self.assertRaises(SystemExit):
                L.require_open_pr("vitalharmony/hrse", "feat/1234-thing")

    def test_an_open_pr_satisfies_the_check(self) -> None:
        with mock.patch.object(
            L, "run", side_effect=_run_returning(_pr_list_result([{"number": 42, "state": "OPEN"}]))
        ):
            checks, warnings = L.require_open_pr("vitalharmony/hrse", "feat/1234-thing")
        self.assertEqual(checks, ["pr-open"])
        self.assertEqual(warnings, [])

    def test_gh_failure_aborts_rather_than_silently_passing(self) -> None:
        """An unestablished PR-open state must never read as a verified one
        -- same posture `refresh_main()` takes for the base-currency check."""
        failed = subprocess.CompletedProcess(("gh",), 1, "", "rate limited")
        with mock.patch.object(L, "run", return_value=failed):
            with self.assertRaises(SystemExit):
                L.require_open_pr("vitalharmony/hrse", "feat/1234-thing")

    def test_override_downgrades_to_a_warning_naming_the_reason(self) -> None:
        """a private-repo incident's own pattern: the override must leave a trace on the
        thread, not just a CLI argument nobody else ever sees."""
        with mock.patch.object(L, "run", side_effect=_run_returning(_pr_list_result([]))):
            checks, warnings = L.require_open_pr(
                "vitalharmony/hrse", "feat/1234-thing",
                ack_no_pr_required="deliberately gating a doc-only branch, no CI needed",
            )
        self.assertEqual(checks, ["pr-open (acknowledged override)"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("deliberately gating a doc-only branch, no CI needed", warnings[0])

    def test_the_query_never_passes_repo_the_issue_repo(self) -> None:
        """Preclose finding: a cross-repo attestation (harmonic-forge#568's
        own --repo/attested-commit-repo split, a private-repo incident) can have
        `repo` name a DIFFERENT repo than the branch/PR actually live in.
        `gh pr list --repo <issue repo>` would then return [] for a PR that
        is genuinely open elsewhere and false-refuse it. a private-repo incident: the repo
        is resolved via `gh repo view` (cwd-based) rather than left to `gh
        pr list`'s own less-reliable implicit detection, but the source of
        truth is the same -- cwd, never the issue's `--repo`."""
        captured: list[tuple] = []

        def _run(*args, **kwargs):
            captured.append(args)
            return _run_returning(
                _pr_list_result([{"number": 1, "state": "OPEN"}]),
                "vitalharmony/hrse")(*args, **kwargs)

        with mock.patch.object(L, "run", side_effect=_run):
            L.require_open_pr("vitalharmony/harmonic-forge", "fix/152-gate-codex-tool")

        # harmonic-forge#220: the REST path. The query call is the one that
        # reaches GitHub -- it must name the CWD's repo and never the issue's.
        # `run()` takes varargs, so each captured entry IS the argv tuple.
        rest_call = next(c for c in captured if "api" in c)
        joined = " ".join(str(p) for p in rest_call)
        self.assertIn("repos/vitalharmony/hrse/pulls", joined)
        self.assertNotIn("vitalharmony/harmonic-forge", joined)
        everything = " ".join(str(p) for c in captured for p in c)
        self.assertNotIn("vitalharmony/harmonic-forge", everything)

    def test_the_rest_path_is_used_and_graphql_is_not_called(self) -> None:
        """harmonic-forge#220, the reason this function was rewritten: a
        session whose GraphQL budget is exhausted (point cost, not call
        count) can still post. `gh repo view` and `gh pr list` are both
        GraphQL-backed, and this check runs BEFORE --ack-no-pr-required is
        consulted -- so when they were the only path, no override existed and
        nothing could be posted at all while a Lane 3 sat blocked."""
        captured: list[tuple] = []

        def _run(*args, **kwargs):
            captured.append(args)
            return _run_returning(
                _pr_list_result([{"number": 1, "state": "OPEN"}]))(*args, **kwargs)

        with mock.patch.object(L, "run", side_effect=_run):
            checks, warnings = L.require_open_pr("vitalharmony/hrse", "feat/x")

        self.assertEqual(checks, ["pr-open"])
        self.assertEqual(warnings, [])
        self.assertTrue(any("remote" in c for c in captured), "git remote was not consulted")
        self.assertTrue(any("api" in c for c in captured), "REST was not used")
        self.assertFalse(any("view" in c for c in captured), "gh repo view (GraphQL) was called")
        self.assertFalse(any("pr" in c and "list" in c for c in captured),
                         "gh pr list (GraphQL) was called")

    def test_it_falls_back_to_graphql_when_git_cannot_resolve_the_repo(self) -> None:
        """The fallback is not dead code: a non-github.com remote, or no git
        remote at all, still has to reach a verdict rather than fail open."""
        captured: list[tuple] = []

        def _run(*args, **kwargs):
            captured.append(args)
            return _run_returning(
                _pr_list_result([{"number": 1, "state": "OPEN"}]),
                git_remote_fails=True)(*args, **kwargs)

        with mock.patch.object(L, "run", side_effect=_run):
            checks, _ = L.require_open_pr("vitalharmony/hrse", "feat/x")

        self.assertEqual(checks, ["pr-open"])
        self.assertTrue(any("view" in c for c in captured),
                        "fallback did not reach gh repo view")

    def test_the_remediation_message_does_not_hardcode_the_issue_repo(self) -> None:
        """The printed `gh pr create` command must be runnable as-is from the
        branch's own checkout -- naming the issue's repo there would tell the
        reader to open a PR for a branch that may not exist in that repo."""
        with mock.patch.object(L, "run", side_effect=_run_returning(_pr_list_result([]))):
            with self.assertRaises(SystemExit) as caught:
                L.require_open_pr("vitalharmony/harmonic-forge", "fix/152-gate-codex-tool")
        message = str(caught.exception) + getattr(caught.exception, "args", ("",))[0]
        self.assertNotIn("--repo vitalharmony/harmonic-forge", message)

    def test_repo_view_failure_aborts_rather_than_silently_passing(self) -> None:
        """a private-repo incident: the new `gh repo view` resolution step must fail
        closed too, same posture as the `gh pr list` call it feeds."""
        failed = subprocess.CompletedProcess(("gh",), 1, "", "not a git repo")
        with mock.patch.object(L, "run", return_value=failed):
            with self.assertRaises(SystemExit):
                L.require_open_pr("vitalharmony/hrse", "feat/1234-thing")


class PostKindGatesOnlyReadyForL3AndAe(unittest.TestCase):
    """AC4: a kind that precedes any Lane 3 gate (handoff, rework) or that
    always follows an already-checked ae (standalone sweep) must not pay this
    check a second time or be refused for lacking a PR it never needed.

    Source-level, matching `test_l1_post_base_currency.py`'s own precedent —
    driving `post_kind()` end to end means also stubbing `static_checks()`'s
    entire ancestry-check chain correctly, and a mis-stubbed call there would
    raise for the wrong reason while still passing an assertRaises(SystemExit),
    proving nothing about the PR check specifically.
    """

    def test_post_kind_calls_require_open_pr_only_for_ready_for_l3_and_ae(self) -> None:
        body = ast.unparse(_fn("post_kind"))
        self.assertIn("kind in ('ready-for-l3', 'ae')", body)
        self.assertIn("require_open_pr(", body)

    def test_the_gate_runs_after_world_checks_not_before(self) -> None:
        """world_checks already does a live sibling-overlap scan; ordering
        the cheaper/more-established check first matches static_checks'
        own cheap-check-first precedent (test_l1_post_base_currency.py)."""
        body = ast.unparse(_fn("post_kind"))

        def _at(needle: str) -> int:
            self.assertIn(needle, body)
            return body.index(needle)

        self.assertLess(_at("world_checks("), _at("require_open_pr("))


class OverrideHeadingsAreDistinct(unittest.TestCase):
    """Preclose finding: a no-PR override was rendered under the
    sibling-overlap heading, asserting on the thread that an overlap was
    acknowledged when none occurred -- the one place this record is read."""

    def test_a_no_pr_override_renders_under_its_own_heading(self) -> None:
        captured = {}

        def fake_comment_body(repo, issue, body):
            captured["body"] = body
            return "https://example/1", 1

        original = (L.comment_body, L.static_checks, L.world_checks,
                    L.write_receipt, L.require_open_pr)
        L.comment_body = fake_comment_body
        L.static_checks = lambda sha, branch: ["body-validation"]
        L.world_checks = lambda *a, **k: ([], [])
        L.require_open_pr = lambda *a, **k: (
            ["pr-open (acknowledged override)"],
            ["- no open PR exists -- acknowledged: doc-only branch, no CI needed"],
        )
        L.write_receipt = lambda record: None
        try:
            L.post_kind("o/r", 1, "ae", "body", "abc", "br")
        finally:
            (L.comment_body, L.static_checks, L.world_checks,
             L.write_receipt, L.require_open_pr) = original

        self.assertIn("### No-open-PR override (operator-acknowledged)", captured["body"])
        self.assertNotIn("### Sibling-overlap override", captured["body"])

    def test_a_sibling_overlap_override_still_renders_under_its_own_heading(self) -> None:
        """The fix must not regress the pre-existing overlap-warning path
        while separating the two."""
        captured = {}

        def fake_comment_body(repo, issue, body):
            captured["body"] = body
            return "https://example/1", 1

        original = (L.comment_body, L.static_checks, L.world_checks,
                    L.write_receipt, L.require_open_pr)
        L.comment_body = fake_comment_body
        L.static_checks = lambda sha, branch: ["body-validation"]
        L.world_checks = lambda *a, **k: (
            [], ["- active sibling branch overlaps -- acknowledged: reason"],
        )
        L.require_open_pr = lambda *a, **k: (["pr-open"], [])
        L.write_receipt = lambda record: None
        try:
            L.post_kind("o/r", 1, "ae", "body", "abc", "br")
        finally:
            (L.comment_body, L.static_checks, L.world_checks,
             L.write_receipt, L.require_open_pr) = original

        self.assertIn("### Sibling-overlap override (operator-acknowledged)", captured["body"])
        self.assertNotIn("### No-open-PR override", captured["body"])


if __name__ == "__main__":
    unittest.main()
