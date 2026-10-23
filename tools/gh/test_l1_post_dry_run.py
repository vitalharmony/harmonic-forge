#!/usr/bin/env python3
"""harmonic-forge#1115: `l1_post.py --dry-run` runs every refusal for one post,
posts nothing, and a clean run is the only preflight that admits that exact
real post in this session."""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import l1_post as L  # noqa: E402
import check_lane3_ready as _lane3  # noqa: E402

REPO, ISSUE = "vitalharmony/harmonic-forge", 1115
SHA_A, SHA_B = "a" * 40, "b" * 40
NEEDLE = "first real run of this tool in this session"
R4L3 = "## Ready for Lane 3 — F1115\n\n**Target:** the dry-run gate\n**Verified:** every kill check\n**Next:** Lane 3 to spec the gate\n"
REWORK = "## Rework — F1115\n\n**Finding:** the branch needs a rebase onto main\n**Next:** Lane 2 to rebase the branch\n"
NO_NEXT = "## Rework — F1115\n\n**Finding:** the branch needs a rebase onto main\n"
AE = ("## AE — F1115\n\n**Authorized:** the approved Lane 3 spec at this SHA\n"
      "**External calls authorized:** none\n**Next:** Lane 3 to run the gate\n")
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
        self.cheap = True  # stub cheap_static_checks; False runs the real one
        self.spec = None  # a spec body: run the real validate_sweep against it
        self.thread: list[dict] = []  # the fake issue thread (AC9's cross-check)

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
            # harmonic-forge#1115 AC9: the thread the AE/sweep cross-check reads.
            mock.patch.object(_lane3, "fetch_comments", side_effect=lambda repo, issue: self.thread),
            mock.patch.object(L, "grant_on_main", side_effect=lambda *a: self.grant),
            mock.patch.object(L, "refresh_main", side_effect=lambda: self.order.append("fetch") or "f" * 40),
            mock.patch.object(L, "world_checks", side_effect=self._world),
            mock.patch.object(L, "_refuse_if_ci_red"),
            mock.patch.object(L, "require_open_pr", side_effect=self._require_pr),
            mock.patch.object(L, "_open_pr", self.open_pr),
            mock.patch.object(L, "static_checks", self.static),
            *([mock.patch.object(L, "cheap_static_checks")] if self.cheap else []),
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
        # harmonic-forge#1134 AC3: the dry run runs (or reuses) the check.
        self.static.assert_called_once()
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
        # The wrapper cds elsewhere (Lane 1's tools worktree), so run from a
        # directory that does NOT hold the body: only HF_CALLER_CWD can find
        # it, and deleting resolve_args fails this test (pass-2 survivor).
        elsewhere = Path(tempfile.mkdtemp(prefix="l1-post-elsewhere-"))
        self.addCleanup(shutil.rmtree, elsewhere, True)
        os.chdir(elsewhere)
        self.addCleanup(os.chdir, cwd)
        self.assertFalse((elsewhere / body.name).exists())
        relative = ("--repo", REPO, "--issue", ISSUE, "--kind", "ready-for-l3", "--sha", SHA_A,
                    "--branch", "fix/1115-x", "--file", body.name, "--dry-run")
        # A wrapper run: the exec line passes the caller's directory, which
        # l1_post resolves the relative --file against (harmonic-forge#1117 AC2).
        with mock.patch.dict(os.environ, {"L1_POST_ENTRY": "mise run l1-post",
                                          "HF_CALLER_CWD": str(self.dir)}):
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
        self.assertNotIn("mise run check skipped", out)
        self.open_pr.assert_not_called()
        self.static.assert_called_once()

    def test_dry_run_ready_for_l3_runs_the_cheap_static_checks(self):
        """Pass 1 survivor 7: only `mise run check` is skipped. A branch that
        no longer resolves to the attested SHA is reported by the dry run, as
        the real post would refuse it."""
        self.cheap = False
        # static_checks runs cheap_static_checks first; keep that real here.
        self.static.side_effect = lambda sha, branch, dry_run=False: (
            L.cheap_static_checks(sha, branch), ([], ("t0", "t1"), {"result": "pass"}))[1]
        with mock.patch.object(L, "resolve_sha", side_effect=lambda value, *a, **k:
                               SHA_B if value == "fix/1115-x" else value):
            code, _, _ = self.run_main(*self.r4l3("--dry-run"))
        self.assertIn("no longer resolves to the attested SHA", code or "")
        self.assertEqual(self.receipts(), [])

    def test_dry_run_refuses_a_failing_check_and_names_the_kept_logs(self):
        """harmonic-forge#1134 AC3."""
        self.static.return_value = ([], ("t0", "t1"), {"result": "fail", "output": "step 25 FAILED",
                                                      "logs": "/kept/logs"})
        code, _, _ = self.run_main(*self.r4l3("--dry-run"))
        self.assertIn("dry run: mise run check failed; step logs kept at /kept/logs", code or "")
        self.assertIn("step 25 FAILED", code or "")
        self.assertEqual(self.receipts(), [])

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
        """AC9: every External-calls refusal arrives in the one numbered list."""
        self.overlap = True
        self.spec = ("## Lane 3 Test Spec — F1115\n\n### Cases\n| TC1 | live | x | y | z |\n"
                     "| TC2 | live | x | y | z |\n\n"
                     "<!-- l1-post v1; kind=spec; posted-by=LANE3; classes=1:live,2:live -->")
        sweep = ("## Gate-readiness sweep — F1115\n\n**Readiness:** ready\n**Blockers:** none\n"
                 "**Next:** Lane 3 to run\n\nWrite tier: W\n\n### Per-case readiness\n"
                 "- TC1: ready.\n  External calls: llm:gemini\n- TC2: ready, live UI\n")
        code, _, _ = self.run_main("--repo", REPO, "--issue", ISSUE, "--kind", "ae-and-sweep", "--sha", SHA_A,
                                   "--branch", "fix/1115-x", "--ae-file", self.body(AE, "ae.md"),
                                   "--sweep-file", self.body(sweep, "sweep.md"),
                                   "--spec-comment", "5", "--dry-run")
        self.assertIn("3 problems", code)
        self.assertIn("TC2 carry no `External calls:` line", code)
        self.assertIn("not authorized: llm:gemini", code)
        self.assertIn(OVERLAP, code)
        self.posted.assert_not_called()
        self.assertEqual(self.receipts(), [])

    def test_a_reserved_marker_does_not_hide_the_other_refusals(self):
        """AC1 (reforge pass 1): the marker refusal is collected with the body
        and world refusals, never raised ahead of them."""
        self.overlap = True
        code, _, _ = self.run_main("--repo", REPO, "--issue", ISSUE, "--kind", "rework", "--sha", SHA_A,
                                   "--branch", "fix/1115-x",
                                   "--file", self.body(NO_NEXT + "<!-- l1-post copied -->\n"), "--dry-run")
        self.assertIn("3 problems", code)
        self.assertIn("reserved l1-post attestation namespace", code)
        self.assertIn("must lead with its summary block", code)
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


#: harmonic-forge#1115 AC10: one row per `_post_digest` input -- (field,
#: kind, base flags, changed flags, body file to rewrite). Flags are a dict,
#: `True` for a switch; a changed flag replaces its base value.
DIGEST_ROWS = [
    ("repo", "ready-for-l3", {}, {"--repo": "vitalharmony/hrse"}, None),
    ("kind", "ready-for-l3", {}, {"--kind": "rework"}, None),
    ("issue", "ready-for-l3", {}, {"--issue": ISSUE + 1}, None),
    ("sha", "ready-for-l3", {}, {"--sha": SHA_B}, None),
    ("branch", "ready-for-l3", {}, {"--branch": "fix/1115-y"}, None),
    ("file", "ready-for-l3", {}, {}, "body.md"),
    ("ae_file", "ae-and-sweep", {}, {}, "ae.md"),
    ("sweep_file", "ae-and-sweep", {}, {}, "sweep.md"),
    ("spec_comment", "ae-and-sweep", {}, {"--spec-comment": 6}, None),
    ("prod_run", "ae", {}, {"--prod-run": "count-label=Person"}, None),
    ("from_sha", "carry-forward", {}, {"--from-sha": "c" * 40}, None),
    ("pass_comment", "carry-forward", {}, {"--pass-comment": 8}, None),
    ("ack_overlap", "ready-for-l3", {}, {"--ack-overlap": "reviewed"}, None),
    ("ack_no_pr_required", "ready-for-l3", {}, {"--ack-no-pr-required": "no CI here"}, None),
    ("milestone_ack", "rework", {}, {"--milestone-ack": "outside the release, operator ruling"}, None),
    ("plan_first", "handoff", {"--plan-first": "false"}, {"--plan-first": "true"}, None),
    ("mutates_live", "handoff", {"--plan-first": "false"}, {"--mutates-live": True}, None),
    ("cross_repo", "handoff", {"--plan-first": "false"}, {"--cross-repo": True}, None),
]


class ReceiptDigestFieldTests(Harness):
    """harmonic-forge#1115 AC10: every receipt-digest input, changed alone
    after a clean dry run, stops the receipt matching. The body validators
    are not under test here, so `_collect` runs nothing; the digest, the
    receipt and the gate are main()'s own."""

    def _argv(self, kind: str, *flag_sets: dict) -> list:
        flags = {"--repo": REPO, "--issue": ISSUE, "--kind": kind, "--sha": SHA_A,
                 "--branch": "fix/1115-x"}
        if kind == "ae-and-sweep":
            flags.update({"--ae-file": self.dir / "ae.md", "--sweep-file": self.dir / "sweep.md",
                          "--spec-comment": 5})
        else:
            flags["--file"] = self.dir / "body.md"
        if kind == "carry-forward":
            flags.update({"--from-sha": "d" * 40, "--pass-comment": 7})
        for extra in flag_sets:
            flags.update(extra)
        argv: list = []
        for flag, value in flags.items():
            argv += [flag] if value is True else [flag, value]
        return argv

    def test_the_rows_cover_every_digest_input(self):
        """A field added to `_post_digest` needs a row here."""
        import argparse  # noqa: PLC0415
        seen: dict = {}
        with mock.patch.object(L.json, "dumps", side_effect=lambda fields, **k: seen.update(fields) or "{}"):
            L._post_digest(argparse.Namespace(**{k: None for k in (
                "issue", "kind", "branch", "file", "ae_file", "sweep_file", "spec_comment", "prod_run",
                "from_sha", "pass_comment", "ack_overlap", "ack_no_pr_required", "milestone_ack",
                "plan_first", "mutates_live", "cross_repo")}), REPO, SHA_A)
        self.assertEqual(set(seen), {row[0] for row in DIGEST_ROWS})

    def test_each_digest_field_invalidates_the_receipt(self):
        for name, kind, base, changed, body in DIGEST_ROWS:
            with self.subTest(field=name), mock.patch.object(L, "_collect"), \
                    mock.patch.object(L, "_carry_forward"), mock.patch.object(L, "refuse_carry_forward"), \
                    mock.patch.dict(os.environ, {"LANE": "1"}):
                for file in ("body.md", "ae.md", "sweep.md"):
                    self.body(f"{file} contents\n", file)
                base_argv = self._argv(kind, base)
                self.assertIsNone(self.run_main(*base_argv, "--dry-run")[0], name)
                if body:
                    self.body(f"{body} changed\n", body)
                    changed_argv = base_argv
                else:
                    changed_argv = self._argv(kind, base, changed)
                    self.assertNotEqual(changed_argv, base_argv)
                code = self.run_main(*changed_argv)[0]
                self.assertIn(NEEDLE, code or "", f"{name}: the changed post matched the old receipt")
                if body:
                    self.body(f"{body} contents\n", body)
                code = self.run_main(*base_argv)[0]
                self.assertNotIn(NEEDLE, code or "", f"{name}: the unchanged post must still match")

if __name__ == "__main__":
    unittest.main()
