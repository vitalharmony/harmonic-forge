#!/usr/bin/env python3
"""harmonic-forge#1115: `l1_post.py --dry-run` runs every refusal for one post,
posts nothing, and a clean run is the only preflight that admits that exact
real post in this session."""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import l1_post as L  # noqa: E402

REPO, ISSUE = "vitalharmony/harmonic-forge", 1115
SHA_A, SHA_B = "a" * 40, "b" * 40
NEEDLE = "first real run of this tool in this session"
R4L3 = "## Ready for Lane 3 — F1115\n\n**Target:** the dry-run gate\n**Verified:** every kill check\n**Next:** Lane 3 to spec the gate\n"
REWORK = "## Rework — F1115\n\n**Finding:** the branch needs a rebase onto main\n**Next:** Lane 2 to rebase the branch\n"
NO_NEXT = "## Rework — F1115\n\n**Finding:** the branch needs a rebase onto main\n"
AE = "## AE — F1115\n\n**Authorized:** the approved Lane 3 spec at this SHA\n**Next:** Lane 3 to run the gate\n"
OVERLAP = "active sibling branch 'feat/9-x' overlaps target files: tools/gh/l1_post.py"
NO_PR = "no open PR exists for 'fix/1115-x' against main"


class Harness(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"PREFLIGHT_RECEIPT_DIR": str(self.dir / "store"),
                                           "CLAUDE_CODE_SESSION_ID": "session-a"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("L1_POST_ENTRY", None)
        self.overlap = False
        self.pr_open = True
        self.order: list[str] = []
        self.posted = mock.Mock(return_value=("https://example/c/1", 1))
        self.open_pr = mock.Mock(return_value=7)
        self.static = mock.Mock(return_value=([], ("t0", "t1"), {"result": "pass"}))
        self.grant = False
        self.spec = None  # a spec body: run the real validate_sweep against it

    def body(self, text: str, name: str = "body.md") -> Path:
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def _world(self, repo, issue, sha, branch, *, ack_overlap=None):
        self.order.append("world")
        if self.overlap and ack_overlap is None:
            L.fail(OVERLAP)
        return ["issue-open"], ([f"- {OVERLAP} -- acknowledged: {ack_overlap}"] if self.overlap else [])

    def _require_pr(self, repo, branch, *, ack_no_pr_required=None, open_missing=False, issue=None,
                    sha=None, cwd=None):
        # Mirrors the real function's three outcomes (tools/gh/l1_post.py require_open_pr).
        if self.pr_open:
            return ["pr-open"], [], []
        if open_missing and ack_no_pr_required is None:
            return [f"pr-opened #{self.open_pr()}"], [], ["- opened"]
        if ack_no_pr_required is None:
            L.fail(f"{NO_PR} -- CI will never run on this SHA until one does.")
        return ["pr-open (acknowledged override)"], ["- ack"], []

    def run_main(self, *argv, real_post_kind: bool = False):
        """(exit text or None, stdout, stderr)."""
        patches = [
            mock.patch.object(sys, "argv", ["l1_post.py", *map(str, argv)]),
            mock.patch.object(L, "resolve_repo", side_effect=lambda value, *a, **k: value),
            mock.patch.object(L, "apply_project_identity"),
            mock.patch.object(L, "resolve_sha", side_effect=lambda value, *a, **k: value),
            mock.patch.object(L, "validate_sticky_wicket"),
            mock.patch.object(L, "validate_milestone_current"),
            mock.patch.object(L, "validate_ae"),
            mock.patch.object(L, "validate_grant_ae"),
            mock.patch.object(L, "validate_auto_ae"),
            *([mock.patch.object(L, "validate_sweep"), mock.patch.object(L, "_spec_body", return_value="")]
              if self.spec is None else [mock.patch.object(L, "_spec_body", return_value=self.spec)]),
            mock.patch.object(L, "_latest_sweep_calls", return_value=[]),
            mock.patch.object(L, "grant_on_main", side_effect=lambda *a: self.grant),
            mock.patch.object(L, "refresh_main", side_effect=lambda: self.order.append("fetch") or "f" * 40),
            mock.patch.object(L, "world_checks", side_effect=self._world),
            mock.patch.object(L, "_refuse_if_ci_red"),
            mock.patch.object(L, "require_open_pr", side_effect=self._require_pr),
            mock.patch.object(L, "_open_pr", self.open_pr),
            mock.patch.object(L, "static_checks", self.static),
            mock.patch.object(L, "record_queue_candidate"),
            mock.patch.object(L, "_emit_attempt"),
            mock.patch.object(L, "write_receipt"),
            mock.patch.object(L, "comment_body", side_effect=AssertionError("comment_body reached")),
        ]
        if not real_post_kind:
            patches.append(mock.patch.object(L, "post_kind", self.posted))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            try:
                L.main()
                code = None
            except SystemExit as exc:
                code = str(exc)
        return code, out.getvalue(), err.getvalue()

    def r4l3(self, *extra, sha=SHA_A, body=None):
        path = body or self.body(R4L3)
        return ("--repo", REPO, "--issue", ISSUE, "--kind", "ready-for-l3", "--sha", sha,
                "--branch", "fix/1115-x", "--file", path, *extra)

    def receipts(self):
        return list((self.dir / "store").rglob("*.json"))


class DryRunTests(Harness):
    def test_dry_run_lists_overlap_and_body_refusals_together(self):
        """AC1."""
        self.overlap = True
        code, _, _ = self.run_main("--repo", REPO, "--issue", ISSUE, "--kind", "rework", "--sha", SHA_A,
                                   "--branch", "fix/1115-x", "--file", self.body(NO_NEXT), "--dry-run")
        self.assertIsNotNone(code)
        self.assertIn("2 problems:", code)
        self.assertIn(OVERLAP, code)
        self.assertIn("**Next:**", code)
        self.posted.assert_not_called()
        self.assertEqual(self.receipts(), [])

    def test_clean_dry_run_records_keyed_receipt(self):
        """AC2."""
        code, out, _ = self.run_main(*self.r4l3("--dry-run"))
        self.assertIsNone(code)
        self.assertIn("dry run clean: ready-for-l3", out)
        self.posted.assert_not_called()
        self.static.assert_not_called()
        receipts = self.receipts()
        self.assertEqual(len(receipts), 1)
        self.assertTrue(receipts[0].read_text().find('"key": "l1_post:') >= 0)

    def test_real_post_without_matching_dry_run_refused(self):
        """AC3a."""
        code, _, _ = self.run_main(*self.r4l3())
        self.assertIn(NEEDLE, code)
        command = code.split(NEEDLE)[1].splitlines()[1].strip()
        self.assertTrue(command.startswith("python3 ") and "l1_post.py" in command, command)
        self.assertTrue(command.endswith("--dry-run"), command)
        self.assertIn(f"--issue {ISSUE} --kind ready-for-l3 --sha {SHA_A}", command)
        self.posted.assert_not_called()
        self.assertIsNone(self.run_main(*self.r4l3("--dry-run"))[0])
        self.assertIsNone(self.run_main(*self.r4l3())[0])
        self.posted.assert_called_once()

    def test_a_plain_l1_post_receipt_does_not_admit(self):
        """AC3a: the gate is keyed to the post, not to the tool."""
        L._preflight.record("l1_post")
        self.assertIn(NEEDLE, self.run_main(*self.r4l3())[0])

    def test_changed_body_ack_or_sha_invalidates_receipt(self):
        """AC3b."""
        body = self.body(R4L3)
        self.assertIsNone(self.run_main(*self.r4l3("--dry-run", body=body))[0])
        body.write_text(R4L3 + "\nmore\n", encoding="utf-8")
        self.assertIn(NEEDLE, self.run_main(*self.r4l3(body=body))[0])
        body.write_text(R4L3, encoding="utf-8")
        self.assertIn(NEEDLE, self.run_main(*self.r4l3("--ack-overlap", "reviewed", body=body))[0])
        self.assertIn(NEEDLE, self.run_main(*self.r4l3(sha=SHA_B, body=body))[0])
        self.assertIsNone(self.run_main(*self.r4l3(body=body))[0])

    def test_sessionless_caller_not_gated(self):
        """AC5."""
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}):
            self.assertIsNone(self.run_main(*self.r4l3())[0])
        self.posted.assert_called_once()

    def test_refusal_names_wrapper_when_invoked_via_wrapper(self):
        with mock.patch.dict(os.environ, {"L1_POST_ENTRY": "mise run l1-post"}):
            code = self.run_main(*self.r4l3())[0]
        command = code.split(NEEDLE)[1].splitlines()[1].strip()
        self.assertTrue(command.startswith("mise run l1-post --repo "), command)
        self.assertTrue(command.endswith("--dry-run"), command)

    def test_wrapper_and_direct_runs_share_digest(self):
        body = self.body(R4L3)
        cwd = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, cwd)
        relative = ("--repo", REPO, "--issue", ISSUE, "--kind", "ready-for-l3", "--sha", SHA_A,
                    "--branch", "fix/1115-x", "--file", body.name, "--dry-run")
        with mock.patch.dict(os.environ, {"L1_POST_ENTRY": "mise run l1-post"}):
            self.assertIsNone(self.run_main(*relative)[0])
        os.chdir(cwd)
        reordered = ("--file", str(body.resolve()), "--branch", "fix/1115-x", "--sha", SHA_A,
                     "--kind", "ready-for-l3", "--issue", ISSUE, "--repo", REPO)
        self.assertIsNone(self.run_main(*reordered)[0])
        self.posted.assert_called_once()

    def test_dry_run_ready_for_l3_missing_pr_is_info(self):
        """AC7."""
        self.pr_open = False
        code, out, _ = self.run_main(*self.r4l3("--dry-run"))
        self.assertIsNone(code)
        self.assertIn("dry run: would open PR for fix/1115-x against main", out)
        self.assertIn("mise run check skipped", out)
        self.open_pr.assert_not_called()
        self.static.assert_not_called()

    def _ae(self, *extra):
        return ("--repo", REPO, "--issue", ISSUE, "--kind", "ae", "--sha", SHA_A,
                "--branch", "fix/1115-x", "--file", self.body(AE), *extra)

    def test_dry_run_ae_missing_pr_refuses(self):
        self.pr_open = False
        code, _, _ = self.run_main(*self._ae("--dry-run"))
        self.assertIn(NO_PR, code)
        self.open_pr.assert_not_called()
        self.assertEqual(self.receipts(), [])

    def test_dry_run_grant_ae_skips_pr_check(self):
        self.pr_open = False
        self.grant = True
        self.assertIsNone(self.run_main(*self._ae("--dry-run"))[0])

    def test_dry_run_fetches_origin_main_before_world_checks(self):
        """AC7."""
        self.assertIsNone(self.run_main(*self.r4l3("--dry-run"))[0])
        self.assertEqual(self.order[:2], ["fetch", "world"])

    def test_ae_and_sweep_dry_run_collects_world_refusal(self):
        self.overlap = True
        code, _, _ = self.run_main("--repo", REPO, "--issue", ISSUE, "--kind", "ae-and-sweep", "--sha", SHA_A,
                                   "--branch", "fix/1115-x", "--ae-file", self.body(AE, "ae.md"),
                                   "--sweep-file", self.body("## Sweep\n", "sweep.md"),
                                   "--spec-comment", "5", "--dry-run")
        self.assertIn(OVERLAP, code)
        self.posted.assert_not_called()

    def test_dry_run_lists_external_call_refusals_with_the_rest(self):
        """AC9: both External-calls refusals arrive in the one numbered list."""
        self.overlap = True
        self.spec = ("## Lane 3 Test Spec — F1115\n\n### Cases\n| TC1 | live | x | y | z |\n"
                     "| TC2 | live | x | y | z |\n\n"
                     "<!-- l1-post v1; kind=spec; posted-by=LANE3; classes=1:live,2:live -->")
        sweep = ("## Gate-readiness sweep — F1115\n\n**Readiness:** ready\n**Blockers:** none\n"
                 "**Next:** Lane 3 to run\n\nWrite tier: W\n\n### Per-case readiness\n"
                 "- TC1: ready. External calls: Gemini generate\n- TC2: ready, live UI\n")
        code, _, _ = self.run_main("--repo", REPO, "--issue", ISSUE, "--kind", "ae-and-sweep", "--sha", SHA_A,
                                   "--branch", "fix/1115-x", "--ae-file", self.body(AE, "ae.md"),
                                   "--sweep-file", self.body(sweep, "sweep.md"),
                                   "--spec-comment", "5", "--dry-run")
        self.assertIn("3 problems", code)
        self.assertIn("TC2 carry no `External calls:` line", code)
        self.assertIn("external call(s): Gemini generate", code)
        self.assertIn(OVERLAP, code)
        self.posted.assert_not_called()
        self.assertEqual(self.receipts(), [])

    def test_real_post_still_runs_world_checks_after_clean_dry_run(self):
        """The dry run gates in front of the real checks; it replaces none."""
        argv = ("--repo", REPO, "--issue", ISSUE, "--kind", "rework", "--sha", SHA_A,
                "--branch", "fix/1115-x", "--file", self.body(REWORK))
        self.assertIsNone(self.run_main(*argv, "--dry-run", real_post_kind=True)[0])
        self.overlap = True  # the world moved after the dry run
        code, _, _ = self.run_main(*argv, real_post_kind=True)
        self.assertIn(OVERLAP, code)


if __name__ == "__main__":
    unittest.main()
