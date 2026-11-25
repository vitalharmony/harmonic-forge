#!/usr/bin/env python3
"""harmonic-forge#894 slice 6 step 3: ready-for-l3's check path follows CI.

Green or pending CI runs `local-check` (or nothing when the SHA declares
none); absent, unknown or an unreadable CI runs the full check; the dry run
makes the same decision from its own read; receipts are reused only by their
own label; the footer names which check ran; every CI read goes through S0.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import l1_post as L  # noqa: E402

SHA = "b" * 40


def _done(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["x"], returncode, stdout, "")


class CheckPath(unittest.TestCase):
    def test_green_and_pending_take_the_local_path(self):
        with mock.patch.object(L, "_audit_sampled", return_value=False):
            for state in ("green", "pending"):
                self.assertEqual(L._check_path(state, real_post=True), ("local", False))
                self.assertEqual(L._check_path(state, real_post=False), ("local", False))

    def test_absent_unknown_and_an_unread_ci_take_the_full_path(self):
        with mock.patch.object(L, "_audit_sampled", return_value=False):
            for state in ("absent", "unknown", None, "red"):
                self.assertEqual(L._check_path(state, real_post=True), ("full", False))

    def test_the_audit_sample_is_drawn_on_the_real_post_only(self):
        with mock.patch.object(L, "_audit_sampled", return_value=True) as sampled:
            self.assertEqual(L._check_path("green", real_post=True), ("full", True))
            self.assertEqual(L._check_path("green", real_post=False), ("local", False))
        self.assertEqual(sampled.call_count, 1)

    def test_the_sampler_is_one_in_audit_rate(self):
        with mock.patch("secrets.randbelow", return_value=0) as draw:
            self.assertTrue(L._audit_sampled())
        draw.assert_called_once_with(L.AUDIT_RATE)
        with mock.patch("secrets.randbelow", return_value=3):
            self.assertFalse(L._audit_sampled())


class DeclaresLocalCheck(unittest.TestCase):
    def test_reads_the_task_from_the_commit(self):
        for text, want in (("[tasks.check]\nrun='x'\n[tasks.local-check]\nrun='y'\n", True),
                           ('[tasks."local-check"]\n', True),
                           ("[tasks.check]\n# [tasks.local-check]\n", False),
                           ("[tasks.local-check-extra]\n", False)):
            with mock.patch.object(L, "run", return_value=_done(text)) as run:
                self.assertEqual(L._declares_local_check(SHA), want, text)
            run.assert_called_once_with("git", "show", f"{SHA}:mise.toml")

    def test_an_unreadable_mise_toml_declares_none(self):
        with mock.patch.object(L, "run", return_value=_done("", 128)):
            self.assertFalse(L._declares_local_check(SHA))


class ReceiptReuseByLabel(unittest.TestCase):
    """C1: the full path reuses l1-post-scratch only; the local path also
    l1-post-scratch-local; never check, local-check or check-full."""

    def _match(self, source, sources=None):
        data = {"repo": "o__r", "sha": SHA, "forge_sha": "f" * 40, "source": source}
        with mock.patch.object(L, "run", return_value=_done("git@github.com:o/r.git\n")), \
             mock.patch.object(L.check_receipt, "lookup", return_value=data), \
             mock.patch.object(L.check_receipt, "repo_slug", return_value="o__r"), \
             mock.patch.object(L.check_receipt, "forge_sha", return_value="f" * 40):
            if sources is None:
                return L._receipt_matches(Path("."), SHA)
            return L._receipt_matches(Path("."), SHA, sources)

    def test_the_full_path_reuses_only_a_full_scratch_receipt(self):
        self.assertEqual(self._match("l1-post-scratch"), "l1-post-scratch")
        for other in ("l1-post-scratch-local", "check", "local-check", "check-full"):
            self.assertIsNone(self._match(other), other)

    def test_the_local_path_also_reuses_a_scratch_local_receipt(self):
        both = (L.check_receipt.L1_POST_SOURCE, L.check_receipt.L1_POST_LOCAL_SOURCE)
        self.assertEqual(self._match("l1-post-scratch-local", both), "l1-post-scratch-local")
        for other in ("check", "local-check", "check-full"):
            self.assertIsNone(self._match(other, both), other)


class StaticChecksLocalMode(unittest.TestCase):
    def _static(self, *, declared, receipt=None, mode="local"):
        calls = []

        def run(*args, **kwargs):
            calls.append(args)
            if args[:2] == ("git", "rev-parse"):
                return _done("/repo\n")
            raise AssertionError(f"unexpected run: {args}")
        with mock.patch.object(L, "cheap_static_checks"), \
             mock.patch.object(L, "run", side_effect=run), \
             mock.patch.object(L, "_receipt_matches", return_value=receipt) as matches, \
             mock.patch.object(L, "_declares_local_check", return_value=declared), \
             mock.patch.object(L._scratch, "scratch_dir",
                               side_effect=AssertionError("a scratch worktree was made")):
            out = L.static_checks(SHA, "feat/1-x", mode=mode)
        return out, calls, matches

    def test_an_undeclared_local_check_runs_nothing(self):
        (checks, timing, check), calls, _ = self._static(declared=False)
        self.assertEqual(check, {"result": "skipped", "mode": "none"})
        self.assertIsNone(timing)
        self.assertIn("local-check:none", checks)
        self.assertFalse(any(a[:2] == ("mise", "run") for a in calls))

    def test_a_scratch_local_receipt_is_reused_as_local_check(self):
        (checks, _t, check), _c, matches = self._static(
            declared=True, receipt=L.check_receipt.L1_POST_LOCAL_SOURCE)
        self.assertEqual(check, {"result": "reused", "mode": "local-check"})
        self.assertIn("local-check:receipt", checks)
        self.assertIn(L.check_receipt.L1_POST_LOCAL_SOURCE, matches.call_args.args[2])

    def test_the_full_path_never_offers_the_local_label(self):
        (_checks, _t, check), _c, matches = self._static(
            declared=True, receipt=L.check_receipt.L1_POST_SOURCE, mode="full")
        self.assertEqual(check, {"result": "reused"})
        self.assertNotIn(L.check_receipt.L1_POST_LOCAL_SOURCE, matches.call_args.args[2])

    def test_a_declared_local_check_runs_the_local_check_task(self):
        ran = []

        def run(*args, **kwargs):
            ran.append(args)
            if args[:2] == ("git", "rev-parse"):
                return _done("/repo\n")
            if args[:3] == ("git", "worktree", "add"):
                return _done()
            return _done()

        class Holder:
            def __enter__(self):
                return Path("/nonexistent-holder")

            def __exit__(self, *a):
                return False
        with mock.patch.object(L, "cheap_static_checks"), \
             mock.patch.object(L, "run", side_effect=run), \
             mock.patch.object(L, "_receipt_matches", return_value=None), \
             mock.patch.object(L, "_declares_local_check", return_value=True), \
             mock.patch.object(L, "resolve_sha", return_value=SHA), \
             mock.patch.object(L, "_source_repo_is_hrse", return_value=False), \
             mock.patch.object(L, "_private_check_tmp", return_value=(None, {})), \
             mock.patch.object(L._scratch, "scratch_dir", return_value=Holder()):
            checks, _t, check = L.static_checks(SHA, "feat/1-x", mode="local")
        self.assertEqual(check, {"result": "pass", "mode": "local-check"})
        self.assertIn(("mise", "run", "local-check"), [a[:3] for a in ran])
        self.assertNotIn(("mise", "run", "check"), [a[:3] for a in ran])
        self.assertEqual(checks[0], "local-check")


class DryRunDecides(unittest.TestCase):
    """Test case 9: the dry run makes the same CI decision; the real post re-decides."""

    def _dry(self, state):
        seen = []

        def static(sha, branch, dry_run=False, **kw):
            seen.append(kw.get("mode", "full"))
            return [], None, {"result": "pass"}
        L._DRY_RUN_CI[SHA] = state
        try:
            with mock.patch.object(L, "static_checks", side_effect=static):
                L._dry_run_check(SHA, "feat/1-x")
        finally:
            L._DRY_RUN_CI.pop(SHA, None)
        return seen

    def test_a_green_dry_run_never_runs_the_full_check(self):
        self.assertEqual(self._dry("green"), ["local"])

    def test_an_absent_dry_run_runs_the_full_check(self):
        self.assertEqual(self._dry("absent"), ["full"])
        self.assertEqual(self._dry(None), ["full"])

    def test_a_pending_with_red_dry_run_runs_the_full_check(self):
        """harmonic-forge#894 post-verdict finding 2 (fourth check): the dry
        run's step, not only `_check_path`, sends a red job beside a running
        `verify` to the full check.

        Sixth post-verdict check: the classification is stored by the
        production step, not seeded into `_DRY_RUN_CI` by hand. The dry run's
        real CI step (`_publish_checks`' `ci`, through the real
        `_refuse_if_ci_red`, `ci_conclusion` and `_judge_runs`; only the run
        read is stubbed) stores the read, and `_dry_run_check` decides from
        what it stored. A step that stored plain `pending` would take the
        local path and fail here."""
        verify = {"name": "verify", "status": "in_progress", "conclusion": None,
                  "workflow_status": "in_progress", "workflow_event": "pull_request",
                  "workflow_path": ".github/workflows/ci.yml"}
        red = {"name": "frontend", "status": "completed", "conclusion": "failure"}
        for runs, stored, want in (([verify, red], L.PENDING_WITH_RED, ["full"]),
                                   ([verify], "pending", ["local"])):
            seen = []

            def static(sha, branch, dry_run=False, _seen=seen, **kw):
                _seen.append(kw.get("mode", "full"))
                return [], None, {"result": "pass"}
            with self.subTest(stored=stored), \
                 mock.patch.object(L, "_cwd_repo_from_git", return_value="o/r"), \
                 mock.patch.object(L, "_ci_reader", return_value=None), \
                 mock.patch.object(L.gate_ci, "_read_check_runs", return_value=(runs, None)), \
                 mock.patch.object(L, "static_checks", side_effect=static):
                L._DRY_RUN_CI.pop(SHA, None)
                try:
                    steps = L._publish_checks("o/r", 1, "ready-for-l3", SHA, "feat/1-x",
                                              ack_overlap=None, ack_no_pr_required=None,
                                              grant_on_main=False)
                    steps[1]()  # the production CI step stores the classification
                    self.assertEqual(L._DRY_RUN_CI[SHA], stored)
                    L._dry_run_check(SHA, "feat/1-x")
                finally:
                    L._DRY_RUN_CI.pop(SHA, None)
                self.assertEqual(seen, want)

    def test_the_dry_run_never_draws_the_audit_sample(self):
        with mock.patch.object(L, "_audit_sampled", side_effect=AssertionError("drawn")):
            self.assertEqual(self._dry("pending"), ["local"])

    def test_the_dry_run_ci_step_records_its_read(self):
        steps = None
        with mock.patch.object(L, "_refuse_if_ci_red", return_value="pending"):
            steps = L._publish_checks("o/r", 1, "ready-for-l3", SHA, "feat/1-x",
                                      ack_overlap=None, ack_no_pr_required=None,
                                      grant_on_main=False)
            steps[1]()
        try:
            self.assertEqual(L._DRY_RUN_CI[SHA], "pending")
        finally:
            L._DRY_RUN_CI.pop(SHA, None)

    def test_a_pending_dry_run_then_an_absent_real_post_runs_the_full_check(self):
        # The real post reads CI itself; a scratch-local receipt from the dry
        # run is never offered to the full path.
        with mock.patch.object(L, "_audit_sampled", return_value=False):
            self.assertEqual(L._check_path("pending", real_post=False)[0], "local")
            self.assertEqual(L._check_path("absent", real_post=True)[0], "full")


class EveryCiReadGoesThroughS0(unittest.TestCase):
    """Test case 11 (ready-for-l3 half): the path read and the footer read."""

    def test_the_path_read_passes_the_s0_reader(self):
        sentinel = object()
        with mock.patch.object(L, "_cwd_repo_from_git", return_value="o/r"), \
             mock.patch.object(L, "_ci_reader", return_value=sentinel) as made, \
             mock.patch.object(L.gate_ci, "ci_conclusion", return_value=("green", "x")) as read:
            self.assertEqual(L._refuse_if_ci_red("o/r", 1, SHA, None), "green")
        made.assert_called_once_with("o/r")
        self.assertIs(read.call_args.kwargs["reader"], sentinel)

    def test_the_reader_is_built_for_the_source_repo_not_the_tracker(self):
        """harmonic-forge#894 sixth post-verdict check: with a distinct issue
        tracker (`repo`) and source repo (the checkout's origin), the S0
        reader is built for the repo CI is queried in. A reader built for the
        tracker while CI is read in the source repo fails here."""
        sentinel = object()
        with mock.patch.object(L, "_cwd_repo_from_git", return_value="source/code"), \
             mock.patch.object(L, "_ci_reader", return_value=sentinel) as made, \
             mock.patch.object(L.gate_ci, "ci_conclusion", return_value=("green", "x")) as read:
            self.assertEqual(L._refuse_if_ci_red("tracker/issues", 1, SHA, None), "green")
        made.assert_called_once_with("source/code")
        self.assertEqual(read.call_args.args[0], "source/code")
        self.assertIs(read.call_args.kwargs["reader"], sentinel)
        # Through the real `_ci_reader`: the reader handed to the CI read is
        # gate_ci's S0 reader for the source repo.
        built = []

        def s0(repo):
            built.append(repo)
            return f"reader:{repo}"
        with mock.patch.object(L, "_cwd_repo_from_git", return_value="source/code"), \
             mock.patch.object(L.gate_ci, "s0_reader", side_effect=s0), \
             mock.patch.object(L.gate_ci, "ci_conclusion", return_value=("green", "x")) as read:
            L._refuse_if_ci_red("tracker/issues", 1, SHA, None)
        self.assertEqual(built, ["source/code"])
        self.assertEqual(read.call_args.kwargs["reader"], "reader:source/code")

    def test_a_red_job_beside_a_running_verify_takes_the_full_check(self):
        """harmonic-forge#894 post-verdict finding 1, through the real
        `ci_conclusion` and `_judge_runs` (only the run read is stubbed): a red
        job beside a running `verify` reads PENDING_WITH_RED, which runs the
        full check; the same snapshot without it reads `pending`, the local path."""
        verify = {"name": "verify", "status": "in_progress", "conclusion": None,
                  "workflow_status": "in_progress", "workflow_event": "pull_request",
                  "workflow_path": ".github/workflows/ci.yml"}
        red = {"name": "frontend", "status": "completed", "conclusion": "failure"}
        for runs, want, path in (([verify, red], L.PENDING_WITH_RED, "full"),
                                 ([verify], "pending", "local")):
            with self.subTest(want=want), \
                 mock.patch.object(L, "_cwd_repo_from_git", return_value="o/r"), \
                 mock.patch.object(L, "_ci_reader", return_value=None), \
                 mock.patch.object(L.gate_ci, "_read_check_runs", return_value=(runs, None)):
                got = L._refuse_if_ci_red("o/r", 1, SHA, None)
                self.assertEqual(got, want)
                self.assertEqual(L._check_path(got, real_post=False)[0], path)
                # The real post too, with the audit sample held off so the
                # local path is reachable (post-verdict finding 3, second check).
                with mock.patch.object(L, "_audit_sampled", return_value=False):
                    self.assertEqual(L._check_path(got, real_post=True), (path, False))

    def test_an_unreadable_ci_fails_open_to_none(self):
        with mock.patch.object(L, "_cwd_repo_from_git", return_value="o/r"), \
             mock.patch.object(L.gate_ci, "ci_conclusion", side_effect=RuntimeError("x")):
            self.assertIsNone(L._refuse_if_ci_red("o/r", 1, SHA, None))

    def test_the_reader_is_gate_ci_s0_for_the_source_repo(self):
        with mock.patch.object(L.gate_ci, "s0_reader", return_value="R") as s0:
            self.assertEqual(L._ci_reader("o/r"), "R")
        s0.assert_called_once_with("o/r")


class FooterNamesTheCheck(unittest.TestCase):
    def _fake_run(self, *args, **kwargs):
        joined = " ".join(args)
        if joined == "git remote get-url origin":
            return _done("https://github.com/o/r.git\n")
        if "pulls?head=" in joined:
            return _done(json.dumps([{"number": 2, "node_id": "PR_1"}]))
        if "issues/1" in joined:
            return _done(json.dumps({"node_id": "I_1"}))
        raise AssertionError(f"unexpected run() call: {args!r}")

    def test_the_footer_records_which_check_ran_and_reads_ci_through_s0(self):
        for mode in ("local-check", "none", "full"):
            with mock.patch.object(L, "run", side_effect=self._fake_run), \
                 mock.patch.object(L, "_cwd_repo_from_git", return_value="o/r"), \
                 mock.patch.object(L, "_ci_reader", return_value="R"), \
                 mock.patch.object(L.gate_ci, "ci_conclusion",
                                   return_value=("pending", "x")) as read:
                marker = L.pr_issue_marker("o/r", 1, "br", SHA, local_check_mode=mode)
            self.assertIn(f" local-check={mode};", marker)
            self.assertIn("ci-snapshot-state=pending;", marker)
            self.assertEqual(read.call_args.kwargs["reader"], "R")

    def test_an_audit_sampled_post_says_so_in_the_footer(self):
        with mock.patch.object(L, "run", side_effect=self._fake_run), \
             mock.patch.object(L, "_cwd_repo_from_git", return_value="o/r"), \
             mock.patch.object(L.gate_ci, "ci_conclusion", return_value=("green", "x")):
            sampled = L.pr_issue_marker("o/r", 1, "br", SHA, local_check_mode="full",
                                        audit_sampled=True)
            plain = L.pr_issue_marker("o/r", 1, "br", SHA, local_check_mode="full")
        self.assertIn(" local-check=full; local-check-audit=sampled;", sampled)
        self.assertNotIn("local-check-audit", plain)

    def test_without_a_mode_the_marker_is_unchanged(self):
        with mock.patch.object(L, "run", side_effect=self._fake_run), \
             mock.patch.object(L, "_cwd_repo_from_git", return_value="o/r"), \
             mock.patch.object(L.gate_ci, "ci_conclusion", return_value=("green", "x")):
            marker = L.pr_issue_marker("o/r", 1, "br", SHA)
        self.assertNotIn(" local-check=", marker)


if __name__ == "__main__":
    unittest.main()
