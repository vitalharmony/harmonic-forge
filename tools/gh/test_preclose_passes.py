#!/usr/bin/env python3
"""harmonic-forge#834 AC7: the per-issue preclose pass cap and patch-id binding.

Runs the real `preclose_check` plan/complete against a scratch git repo
(`ScratchRepo`, from `test_preclose_check.py`) and the real merge hook's
receipt check, so the two enforcement points are proven to agree on one
receipt shape rather than each against its own mock.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "hooks"))

import preclose_passes  # noqa: E402
from test_preclose_check import ANCHORED, CROSS, FALLBACK, ScratchRepo, _Args, git, preclose  # noqa: E402

import block_missing_preclose_inspection as hook  # noqa: E402

REPO, ISSUE = "vitalharmony/hrse", 1208
SURVIVOR = [ANCHORED]


class PassCapTests(ScratchRepo):
    def run_pass(self, relpath: str, findings: list | None = None, force: bool = False) -> None:
        self.commit(relpath)
        self.plan(tier="fast", force=force)
        self.complete(findings=findings, not_triggered=bool(findings), force=force)

    def receipt(self) -> dict:
        return preclose.find_receipt(REPO, ISSUE)

    def rebase_onto_moved_base(self) -> None:
        """Main moves under the branch: an unrelated commit on `base`, then a
        rebase. New head SHA, identical `base...head` diff."""
        git("checkout", "-q", "base", cwd=self.repo)
        (self.repo / "unrelated.txt").write_text("u\n")
        git("add", "-A", cwd=self.repo)
        git("commit", "-qm", "unrelated", cwd=self.repo)
        git("checkout", "-q", "main", cwd=self.repo)
        git("rebase", "-q", "base", cwd=self.repo)

    def test_receipt_records_count_survivors_and_patch_id(self) -> None:
        self.run_pass("scripts/a.py", SURVIVOR)
        stored = self.receipt()
        self.assertEqual(stored["pass_count"], 1)
        self.assertEqual(stored["surviving_findings"], 1)
        self.assertEqual(stored["reviewed_patch_id"], preclose.local_patch_id("base", "HEAD"))

    def test_planning_a_second_pass_does_not_reset_the_count(self) -> None:
        self.run_pass("scripts/a.py", SURVIVOR)
        self.commit("scripts/b.py")
        self.plan(tier="fast")
        self.assertEqual(self.receipt()["status"], "planned")
        self.assertEqual(self.receipt()["pass_count"], 1)

    def test_patch_identical_rebase_is_not_a_new_pass(self) -> None:
        self.run_pass("scripts/a.py")
        self.rebase_onto_moved_base()
        with self.assertRaises(SystemExit) as caught:
            self.plan(tier="fast")
        self.assertIn("patch-identical", str(caught.exception))
        self.assertEqual(self.receipt()["pass_count"], 1)

    def test_third_pass_with_survivors_on_both_names_sticky_wicket(self) -> None:
        self.run_pass("scripts/a.py", SURVIVOR)
        self.run_pass("scripts/b.py", SURVIVOR)
        self.commit("scripts/c.py")
        with self.assertRaises(SystemExit) as caught:
            self.plan(tier="fast")
        self.assertIn("sticky-wicket", str(caught.exception))

    def test_third_pass_after_a_clean_pass_escalates_to_the_operator(self) -> None:
        self.run_pass("scripts/a.py", SURVIVOR)
        self.run_pass("scripts/b.py")
        self.commit("scripts/c.py")
        with self.assertRaises(SystemExit) as caught:
            self.plan(tier="fast")
        message = str(caught.exception)
        self.assertIn("Escalate to the operator", message)
        self.assertNotIn("sticky-wicket", message)

    def test_complete_is_capped_too(self) -> None:
        self.run_pass("scripts/a.py", SURVIVOR)
        self.run_pass("scripts/b.py", SURVIVOR)
        self.commit("scripts/c.py")
        with self.assertRaises(SystemExit):
            self.complete(findings=SURVIVOR, not_triggered=True)

    def record_post_verdict(self) -> None:
        """What `--post-verdict` writes, minus the cross-family call itself."""
        prior = self.receipt()
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.repo, text=True).strip()
        preclose.write_receipt(REPO, ISSUE, preclose_passes.reviewed_head(prior), 1, "complete",
                               extra=preclose_passes.post_verdict_fields(
                                   prior, "base", head, None, "Red-team provenance: cross-family (x)", 0))

    def test_force_bypasses_and_records_pass_three(self) -> None:
        self.run_pass("scripts/a.py", SURVIVOR)
        self.run_pass("scripts/b.py", SURVIVOR)
        self.commit("scripts/c.py")
        self.record_post_verdict()
        self.plan(tier="fast", force=True)
        self.complete(findings=SURVIVOR, not_triggered=True, force=True)
        self.assertEqual(self.receipt()["pass_count"], 3)

    def test_force_in_the_sticky_wicket_case_needs_the_post_verdict_check(self) -> None:
        """harmonic-forge#838 AC5."""
        self.run_pass("scripts/a.py", SURVIVOR)
        self.run_pass("scripts/b.py", SURVIVOR)
        self.commit("scripts/c.py")
        with self.assertRaises(SystemExit) as refused:
            self.plan(tier="fast", force=True)
        self.assertIn("--post-verdict", str(refused.exception))

    def test_post_verdict_check_is_not_a_pass_and_survives_later_writes(self) -> None:
        self.run_pass("scripts/a.py", SURVIVOR)
        self.run_pass("scripts/b.py", SURVIVOR)
        before = self.receipt()
        self.commit("scripts/c.py")
        self.record_post_verdict()
        after = self.receipt()
        self.assertEqual(after["pass_count"], 2)
        self.assertEqual(preclose_passes.reviewed_head(after), preclose_passes.reviewed_head(before))
        self.plan(tier="fast", force=True)
        self.assertIn("post_verdict_check", self.receipt())

    def post_verdict(self, base: str, findings: list | None = None) -> None:
        """The real `--post-verdict` entry point, with a structurally valid
        cross-family envelope (the same stand-in `complete()` uses)."""
        import json as _json
        envelope = Path(self.findings_file([])).with_name("pv-envelope.txt")
        envelope.write_text(_json.dumps({
            "status": "ok", "label": CROSS, "report": {"assumptions": [{"verdict": "confirmed"}]},
            "family": "codex", "posture": "verify", "exit_code": 0, "caller_family": "claude",
            "target_family": "codex",
            "native": [{"type": "thread.started"}, {"type": "item.completed", "item": {"type": "agent_message"}}]}))
        preclose.post_verdict(_Args(repo=REPO, issue=ISSUE, base=base, head="HEAD", main="base",
                                    findings=self.findings_file(findings or []), envelope=str(envelope)))

    def head(self) -> str:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.repo, text=True).strip()

    def test_post_verdict_entry_point_unlocks_force_for_the_patch_only(self) -> None:
        """F838 preclose pass 1: the real entry point, bound to the pass-2 head."""
        self.run_pass("scripts/a.py", SURVIVOR)
        self.run_pass("scripts/b.py", SURVIVOR)
        pass_two = self.head()
        self.commit("scripts/c.py")
        self.post_verdict(pass_two)
        receipt = self.receipt()
        self.assertEqual(receipt["post_verdict_check"]["base_sha"], pass_two)
        self.assertEqual(receipt["post_verdict_check"]["head_sha"], self.head())
        self.assertEqual(receipt["pass_count"], 2)
        self.plan(tier="fast", force=True)

    def test_post_verdict_refuses_a_base_other_than_the_pass_two_head(self) -> None:
        self.run_pass("scripts/a.py", SURVIVOR)
        self.run_pass("scripts/b.py", SURVIVOR)
        self.commit("scripts/c.py")
        with self.assertRaises(SystemExit) as refused:
            self.post_verdict("base")  # the whole branch, not the patch
        self.assertIn("pass-2 head", str(refused.exception))
        self.assertNotIn("post_verdict_check", self.receipt())

    def test_post_verdict_refuses_an_empty_patch(self) -> None:
        self.run_pass("scripts/a.py", SURVIVOR)
        self.run_pass("scripts/b.py", SURVIVOR)
        with self.assertRaises(SystemExit) as refused:
            self.post_verdict(self.head())
        self.assertIn("no patch", str(refused.exception))

    def test_post_verdict_outside_the_sticky_wicket_case_is_refused(self) -> None:
        self.run_pass("scripts/a.py", SURVIVOR)
        pass_one = self.head()
        self.commit("scripts/b.py")
        with self.assertRaises(SystemExit):
            self.post_verdict(pass_one)

    def test_post_verdict_accepts_a_survivor_without_mechanism(self) -> None:
        self.run_pass("scripts/a.py", SURVIVOR)
        self.run_pass("scripts/b.py", SURVIVOR)
        pass_two = self.head()
        self.commit("scripts/c.py")
        self.post_verdict(pass_two, [{"anchor": "x.py:1", "scenario": "breaks"}])
        self.assertEqual(self.receipt()["post_verdict_check"]["surviving"], 1)

    def envelope(self, label: str = CROSS) -> str:
        import json as _json
        path = Path(self.findings_file([])).with_name("cli-envelope.txt")
        ok = label == CROSS
        body = {"status": "ok" if ok else "process-error", "label": label,
                "exit_code": 0 if ok else 1}
        if ok:
            body.update({"report": {"assumptions": [{"verdict": "confirmed"}]}, "family": "codex",
                         "posture": "verify", "caller_family": "claude", "target_family": "codex",
                         "native": [{"type": "thread.started"},
                                    {"type": "item.completed", "item": {"type": "agent_message"}}]})
        path.write_text(_json.dumps(body))
        return str(path)

    def cli_post_verdict(self, base: str, label: str = CROSS) -> None:
        """Through `main()`'s --post-verdict dispatch, not the function."""
        argv = ["preclose_check.py", "--repo", REPO, "--issue", str(ISSUE), "--post-verdict",
                "--base", base, "--main", "base", "--findings", self.findings_file([]),
                "--envelope", self.envelope(label)]
        with patch.object(sys, "argv", argv), self.assertRaises(SystemExit) as done:
            preclose.main()
        if done.exception.code not in (0, None):
            raise SystemExit(done.exception.code)

    def test_cli_accepts_a_patch_identical_base_after_a_rebase(self) -> None:
        """F838 sticky-wicket PATCH: Lane 1 rebases; the pass-2 commit is gone
        from the branch, and the rebased twin must still be accepted."""
        self.run_pass("scripts/a.py", SURVIVOR)
        self.run_pass("scripts/b.py", SURVIVOR)
        pass_two = self.head()
        self.rebase_onto_moved_base()
        rebased_twin = self.head()
        self.assertNotEqual(rebased_twin, pass_two)
        self.commit("scripts/c.py")
        self.cli_post_verdict(rebased_twin)
        check = self.receipt()["post_verdict_check"]
        self.assertEqual((check["base_sha"], check["head_sha"]), (rebased_twin, self.head()))
        self.plan(tier="fast", force=True)

    def test_cli_refuses_a_fallback_envelope(self) -> None:
        """This check is the one refuter: a call that did not run is not a check."""
        self.run_pass("scripts/a.py", SURVIVOR)
        self.run_pass("scripts/b.py", SURVIVOR)
        pass_two = self.head()
        self.commit("scripts/c.py")
        with self.assertRaises(SystemExit) as refused:
            self.cli_post_verdict(pass_two, label=FALLBACK)
        self.assertIn("cross-family call that ran", str(refused.exception))
        self.assertNotIn("post_verdict_check", self.receipt())

    def test_cli_refuses_an_empty_patch_between_distinct_commits(self) -> None:
        self.run_pass("scripts/a.py", SURVIVOR)
        self.run_pass("scripts/b.py", SURVIVOR)
        pass_two = self.head()
        git("commit", "-q", "--allow-empty", "-m", "empty", cwd=self.repo)
        with self.assertRaises(SystemExit) as refused:
            self.cli_post_verdict(pass_two)
        self.assertIn("no patch", str(refused.exception))

    def test_force_after_a_clean_pass_needs_no_post_verdict_check(self) -> None:
        """The operator case is unchanged: AC5 is the sticky-wicket case only."""
        self.run_pass("scripts/a.py", SURVIVOR)
        self.run_pass("scripts/b.py", [])
        self.commit("scripts/c.py")
        self.plan(tier="fast", force=True)

    def test_count_survives_an_archive_failure(self) -> None:
        """The archive swallows its own errors (harmonic-forge#826); break its
        import for the whole second pass and the count must still be 2."""
        self.run_pass("scripts/a.py", SURVIVOR)
        with patch.dict(sys.modules, {"archive": None}):
            self.run_pass("scripts/b.py", SURVIVOR)
        self.assertEqual(self.receipt()["pass_count"], 2)

    def test_legacy_receipt_without_a_count_is_one_pass(self) -> None:
        legacy = {"repo": REPO, "issue": ISSUE, "reviewed_sha": "old", "status": "complete",
                  "refuters": 3, "surviving_findings": 2}
        self.assertEqual(len(preclose_passes.history(legacy)), 1)
        self.assertEqual(preclose_passes.record(legacy, "new", "pid", 1)["pass_count"], 2)


class MechanismClusterTests(ScratchRepo):
    def receipt(self) -> dict:
        return preclose.find_receipt(REPO, ISSUE)

    def findings(self, *mechanisms: str) -> list[dict]:
        return [{"anchor": f"scripts/{number}.py:1", "scenario": "breaks",
                 "mechanism": mechanism}
                for number, mechanism in enumerate(mechanisms)]

    def run_pass(self, relpath: str, findings: list[dict]) -> str:
        self.commit(relpath)
        self.plan(tier="fast")
        return self.complete(findings=findings, not_triggered=True)

    def test_pass_one_cluster_normalizes_case_and_whitespace_and_routes(self) -> None:
        output = self.run_pass("scripts/a.py", self.findings(
            "Re-parsing shell text", "re-parsing  shell text", "RE-PARSING shell text"))
        cluster = self.receipt()["mechanism_cluster"]
        self.assertEqual(cluster["mechanisms"], ["re-parsing shell text"])
        self.assertEqual(self.receipt()["pass_history"][0]["mechanisms"],
                         ["re-parsing shell text"])
        self.assertIn("sticky-wicket", output)

    def test_different_and_near_synonym_mechanisms_do_not_cluster(self) -> None:
        output = self.run_pass("scripts/a.py", self.findings(
            "re-parse shell text", "re-parsing shell text"))
        self.assertNotIn("mechanism_cluster", self.receipt())
        self.assertIn("re-parse shell text, re-parsing shell text", output)
        self.commit("scripts/b.py")
        self.plan(tier="fast")

    def test_dismissed_finding_does_not_create_a_cluster(self) -> None:
        findings = self.findings("same", "same")
        findings[1]["dismissed"] = "not in diff"
        self.run_pass("scripts/a.py", findings)
        self.assertNotIn("mechanism_cluster", self.receipt())

    def test_unresolved_cluster_refuses_plan_and_complete_but_force_bypasses(self) -> None:
        self.run_pass("scripts/a.py", self.findings("same", "same"))
        self.commit("scripts/b.py")
        with self.assertRaises(SystemExit) as plan_refused:
            self.plan(tier="fast")
        self.assertIn("cluster-verdict", str(plan_refused.exception))
        with self.assertRaises(SystemExit) as complete_refused:
            self.complete(findings=self.findings("other"), not_triggered=True)
        self.assertIn("cluster-verdict", str(complete_refused.exception))
        self.plan(tier="fast", force=True)

    def test_patch_allows_pass_two_and_cluster_survives_plan_and_complete(self) -> None:
        self.run_pass("scripts/a.py", self.findings("same", "same"))
        receipt = self.receipt()
        receipt["mechanism_cluster"].update(verdict="PATCH", comment_url="https://example.test")
        preclose.write_receipt(REPO, ISSUE, receipt["reviewed_sha"], receipt["refuters"],
                               receipt["status"], extra=receipt)
        self.commit("scripts/b.py")
        self.plan(tier="fast")
        self.assertEqual(self.receipt()["mechanism_cluster"]["verdict"], "PATCH")
        self.complete(findings=self.findings("same", "same"), not_triggered=True)
        self.assertEqual(self.receipt()["pass_count"], 2)
        self.assertEqual(self.receipt()["mechanism_cluster"]["verdict"], "PATCH")

    def test_reforge_verdict_requires_force_reforge_and_new_epoch_drops_cluster(self) -> None:
        self.run_pass("scripts/a.py", self.findings("same", "same"))
        receipt = self.receipt()
        receipt["mechanism_cluster"].update(verdict="REFORGE", comment_url="https://example.test")
        preclose.write_receipt(REPO, ISSUE, receipt["reviewed_sha"], receipt["refuters"],
                               receipt["status"], extra=receipt)
        git("checkout", "-q", "-b", "v2", "base", cwd=self.repo)
        self.commit("scripts/new.py")
        with self.assertRaises(SystemExit):
            self.plan(tier="fast")
        with self.assertRaises(SystemExit) as forced_plan:
            self.plan(tier="fast", force=True)
        self.assertIn("--force --reforge", str(forced_plan.exception))
        with self.assertRaises(SystemExit) as forced_complete:
            self.complete(findings=self.findings("new"), not_triggered=True, force=True)
        self.assertIn("--force --reforge", str(forced_complete.exception))
        receipt = self.receipt()
        self.assertIn("--force --reforge", preclose_passes.refusal(
            receipt, preclose.run("git", "rev-parse", "HEAD").stdout.strip(),
            preclose.local_patch_id("base", "HEAD"), force=True))
        self.plan(tier="fast", force=True, reforge=True)
        preclose.complete(_Args(repo=REPO, issue=ISSUE, base="base", head="HEAD",
                                findings=self.findings_file(self.findings("new")), envelope=None,
                                not_triggered=True, cross_family=False, force=True, reforge=True))
        self.assertNotIn("mechanism_cluster", self.receipt())

    def test_pass_two_cluster_does_not_create_the_early_route(self) -> None:
        self.run_pass("scripts/a.py", self.findings("one", "two"))
        self.run_pass("scripts/b.py", self.findings("same", "same"))
        self.assertNotIn("mechanism_cluster", self.receipt())
        self.commit("scripts/c.py")
        with self.assertRaises(SystemExit) as refused:
            self.plan(tier="fast")
        self.assertEqual(str(refused.exception).count("Two preclose passes"), 1)

    def test_cluster_verdict_validates_comment_and_preserves_receipt(self) -> None:
        self.run_pass("scripts/a.py", self.findings("same", "same"))
        before = self.receipt()
        api = subprocess.CompletedProcess([], 0, stdout=(
            '{"issue_url":"https://api.github.com/repos/vitalharmony/hrse/issues/1208",'
            '"html_url":"https://github.com/vitalharmony/hrse/issues/1208#issuecomment-99"}'),
            stderr="")
        args = _Args(repo=REPO, issue=ISSUE, cluster_verdict="PATCH",
                     comment_url="https://www.github.com/vitalharmony/hrse/pull/9#issuecomment-99?x=1",
                     force=False)
        with patch.object(preclose, "run", return_value=api):
            preclose.cluster_verdict(args)
        after = self.receipt()
        self.assertEqual(after["mechanism_cluster"]["verdict"], "PATCH")
        self.assertEqual(after["reviewed_sha"], before["reviewed_sha"])
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(after["refuters"], before["refuters"])

    def test_cluster_verdict_refuses_foreign_comment_api_failure_and_overwrite(self) -> None:
        self.run_pass("scripts/a.py", self.findings("same", "same"))
        args = _Args(repo=REPO, issue=ISSUE, cluster_verdict="PATCH",
                     comment_url="https://github.com/x/y/issues/1#issuecomment-99", force=False)
        foreign = subprocess.CompletedProcess([], 0, stdout=(
            '{"issue_url":"https://api.github.com/repos/x/y/issues/1",'
            '"html_url":"https://github.com/x/y/issues/1#issuecomment-99"}'), stderr="")
        with patch.object(preclose, "run", return_value=foreign), self.assertRaises(SystemExit):
            preclose.cluster_verdict(args)
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="404")
        with patch.object(preclose, "run", return_value=failed), self.assertRaises(SystemExit):
            preclose.cluster_verdict(args)

    def test_cluster_verdict_refuses_no_receipt_no_cluster_and_already_set(self) -> None:
        args = _Args(repo=REPO, issue=ISSUE, cluster_verdict="PATCH",
                     comment_url="https://github.com/vitalharmony/hrse/issues/1208#issuecomment-99",
                     force=False)
        with self.assertRaises(SystemExit) as absent:
            preclose.cluster_verdict(args)
        self.assertIn("prior receipt", str(absent.exception))
        self.run_pass("scripts/a.py", self.findings("one", "two"))
        with self.assertRaises(SystemExit) as no_cluster:
            preclose.cluster_verdict(args)
        self.assertIn("no mechanism cluster", str(no_cluster.exception))

        self.commit("scripts/b.py")
        self.complete(findings=self.findings("same", "same"), not_triggered=True)
        # A pass-2 cluster deliberately does not count. Seed a current-epoch
        # cluster to isolate the verdict-overwrite contract.
        receipt = self.receipt()
        receipt["mechanism_cluster"] = {"pass_sha": receipt["reviewed_sha"], "epoch": 0,
                                        "mechanisms": ["same"], "verdict": "PATCH",
                                        "comment_url": "https://old.invalid"}
        preclose.write_receipt(REPO, ISSUE, receipt["reviewed_sha"], receipt["refuters"],
                               receipt["status"], extra=receipt)
        with self.assertRaises(SystemExit) as set_already:
            preclose.cluster_verdict(args)
        self.assertIn("already has a verdict", str(set_already.exception))

        api = subprocess.CompletedProcess([], 0, stdout=(
            '{"issue_url":"https://api.github.com/repos/vitalharmony/hrse/issues/1208",'
            '"html_url":"https://github.com/vitalharmony/hrse/issues/1208#issuecomment-99"}'),
            stderr="")
        args.force = True
        args.cluster_verdict = "REFORGE"
        with patch.object(preclose, "run", return_value=api):
            preclose.cluster_verdict(args)
        self.assertEqual(self.receipt()["mechanism_cluster"]["verdict"], "REFORGE")

    def test_cluster_verdict_cli_requires_comment_url_and_never_reaches_plan(self) -> None:
        argv = ["preclose_check.py", "--repo", REPO, "--issue", str(ISSUE),
                "--cluster-verdict", "PATCH"]
        with patch.object(sys, "argv", argv), patch.object(preclose, "plan") as planned, \
                self.assertRaises(SystemExit) as refused:
            preclose.main()
        self.assertEqual(refused.exception.code, 2)
        planned.assert_not_called()

    def test_concurrent_verdict_correction_cannot_be_overwritten_by_delayed_patch(self) -> None:
        self.run_pass("scripts/a.py", self.findings("same", "same"))
        patch_args = _Args(repo=REPO, issue=ISSUE, cluster_verdict="PATCH",
                           comment_url="https://github.com/vitalharmony/hrse/issues/1208#issuecomment-99",
                           force=False)
        reforge_args = _Args(repo=REPO, issue=ISSUE, cluster_verdict="REFORGE",
                             comment_url=patch_args.comment_url, force=True)
        entered_api = threading.Event()
        release_patch = threading.Event()
        original_run = preclose.run
        api = subprocess.CompletedProcess([], 0, stdout=(
            '{"issue_url":"https://api.github.com/repos/vitalharmony/hrse/issues/1208",'
            '"html_url":"https://github.com/vitalharmony/hrse/issues/1208#issuecomment-99"}'),
            stderr="")

        def interleaved_run(*args, **kwargs):
            if args[:2] == ("gh", "api"):
                if threading.current_thread().name == "delayed-patch":
                    entered_api.set()
                    self.assertTrue(release_patch.wait(5))
                return api
            return original_run(*args, **kwargs)

        errors: list[BaseException] = []

        def invoke(args):
            try:
                preclose.cluster_verdict(args)
            except BaseException as exc:  # surfaced in the test thread below
                errors.append(exc)

        with patch.object(preclose, "run", side_effect=interleaved_run):
            delayed = threading.Thread(target=invoke, args=(patch_args,), name="delayed-patch")
            correction = threading.Thread(target=invoke, args=(reforge_args,), name="newer-reforge")
            delayed.start()
            self.assertTrue(entered_api.wait(5))
            correction.start()
            time.sleep(0.05)
            release_patch.set()
            delayed.join(5)
            correction.join(5)
        self.assertFalse(delayed.is_alive() or correction.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.receipt()["mechanism_cluster"]["verdict"], "REFORGE")


class PreclosePassOneFixTests(ScratchRepo):
    """The five defects harmonic-forge#834's own preclose pass 1 found."""

    def run_pass(self, relpath: str, findings: list | None = None) -> None:
        PassCapTests.run_pass(self, relpath, findings)

    def complete_reforge(self, force: bool = True) -> str:
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            preclose.complete(_Args(repo=REPO, issue=ISSUE, base="base", head="HEAD",
                                    findings=self.findings_file(SURVIVOR), envelope=None,
                                    not_triggered=True, cross_family=False, force=force,
                                    reforge=True))
        return out.getvalue()

    def test_a_reindent_is_a_different_diff(self) -> None:
        """A: `--stable` hashed a behavior-changing dedent as the same diff."""
        self.commit("scripts/g.py", "def g(x):\n    if x:\n        a()\n        b()\n")
        self.plan(tier="fast")
        self.complete()
        self.commit("scripts/g.py", "def g(x):\n    if x:\n        a()\n    b()\n")
        diff = preclose.run("git", "diff", "base...HEAD").stdout
        with patch.object(hook, "_gh", return_value=diff):
            self.assertFalse(hook._preclose_receipt_ok(REPO, str(ISSUE), "new-head", "7"))
        self.assertIn("refuters:", self.plan(tier="fast"))

    def test_operator_reforge_on_a_changed_diff_restarts_the_count(self) -> None:
        """B: the reforge exit exists, as the operator's instruction (--force)."""
        self.run_pass("scripts/a.py", SURVIVOR)
        self.run_pass("scripts/b.py", SURVIVOR)
        git("checkout", "-q", "-b", "v2", "base", cwd=self.repo)
        self.commit("scripts/new.py")
        self.assertIn("refuters:", self.plan(tier="fast", reforge=True, force=True))
        out = self.complete_reforge()
        self.assertIn("recorded pass 1 of at most 2", out)
        stored = preclose.find_receipt(REPO, ISSUE)
        self.assertEqual(stored["pass_count"], 1)
        self.assertEqual(len(stored["pass_history"]), 3)

    def test_reforge_without_force_is_refused(self) -> None:
        """F834 pass 2 / sticky-wicket: no branch-name fence; reforge is operator-only."""
        self.run_pass("scripts/a.py", SURVIVOR)
        self.run_pass("scripts/b.py", SURVIVOR)
        for move in (("checkout", "-q", "-b", "v2"), ("checkout", "-q", "--detach"),
                     ("branch", "-m", "main", "renamed")):
            with self.subTest(move=move):
                git(*move, cwd=self.repo)
                with self.assertRaises(SystemExit) as caught:
                    self.plan(tier="fast", reforge=True)
                self.assertIn("operator's instruction", str(caught.exception))

    def test_operator_reforge_on_an_already_reviewed_diff_is_refused(self) -> None:
        self.run_pass("scripts/a.py", SURVIVOR)
        self.run_pass("scripts/b.py", SURVIVOR)
        git("checkout", "-q", "-b", "v2", cwd=self.repo)
        with self.assertRaises(SystemExit) as caught:
            self.plan(tier="fast", reforge=True, force=True)
        self.assertIn("needs a changed diff", str(caught.exception))

    def test_abandoned_plan_does_not_unreview_the_last_pass(self) -> None:
        """C: pass 1 at A, pass 2 planned at B, B withdrawn: A still merges."""
        self.run_pass("scripts/a.py")
        reviewed_sha = preclose.find_receipt(REPO, ISSUE)["reviewed_sha"]
        self.commit("scripts/b.py")
        self.plan(tier="fast")
        git("reset", "-q", "--hard", reviewed_sha, cwd=self.repo)
        self.assertEqual(preclose.find_receipt(REPO, ISSUE)["status"], "planned")
        self.assertTrue(hook._preclose_receipt_ok(REPO, str(ISSUE), reviewed_sha, "7"))

    def test_local_diff_config_does_not_change_the_patch_id(self) -> None:
        """D: the script's rendering must match `gh pr diff`'s, whatever diff.* says."""
        self.commit("scripts/a.py", "".join(f"line {n}\n" for n in range(40)))
        before = preclose.local_patch_id("base", "HEAD")
        for key, value in (("diff.context", "10"), ("diff.noprefix", "true"),
                           ("diff.mnemonicPrefix", "true")):
            git("config", key, value, cwd=self.repo)
        self.assertEqual(preclose.local_patch_id("base", "HEAD"), before)

    def test_hook_at_the_cap_with_survivors_names_sticky_wicket(self) -> None:
        """E: the hook's sticky-wicket half of AC4 had no test."""
        self.run_pass("scripts/a.py", SURVIVOR)
        self.run_pass("scripts/b.py", SURVIVOR)
        message = hook._stale_receipt_message(REPO, str(ISSUE), "7", "abc")
        self.assertIn("sticky-wicket", message)
        self.assertNotIn("Run the pre-close pass", message)


class OneReaderInvariantTests(unittest.TestCase):
    """F834 sticky-wicket change 5: a receipt field read outside
    preclose_passes.py is how batch_preflight.py drifted. Enforced, not remembered."""

    def test_no_receipt_field_is_read_outside_the_shared_module(self) -> None:
        import re
        tools = HERE.parent
        reads = re.compile(r"""(?:\.get\(|\[)["'](?:reviewed_sha|reviewed_patch_id|pass_history|pass_count)["']""")
        offenders = []
        for path in tools.rglob("*.py"):
            if path.name.startswith("test_") or path.name == "preclose_passes.py":
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if reads.search(line):
                    offenders.append(f"{path.relative_to(tools)}:{number}")
        self.assertEqual(offenders, [])


class HookPatchIdTests(ScratchRepo):
    """AC3/AC4 through the real hook and the real receipt."""

    def setUp(self) -> None:
        super().setUp()
        self.commit("scripts/a.py")
        self.plan(tier="fast")
        self.complete()
        self.reviewed = preclose.find_receipt(REPO, ISSUE)["reviewed_sha"]

    def pr_diff(self) -> str:
        return preclose.run("git", "diff", "base...HEAD").stdout

    def test_patch_identical_rebase_is_accepted(self) -> None:
        PassCapTests.rebase_onto_moved_base(self)
        with patch.object(hook, "_gh", return_value=self.pr_diff()):
            self.assertTrue(hook._preclose_receipt_ok(REPO, str(ISSUE), "new-head-sha", "7"))

    def test_real_diff_change_is_denied(self) -> None:
        self.commit("scripts/extra.py")
        with patch.object(hook, "_gh", return_value=self.pr_diff()):
            self.assertFalse(hook._preclose_receipt_ok(REPO, str(ISSUE), "new-head-sha", "7"))

    def test_batch_preflight_agrees_with_the_merge_hook_after_a_rebase(self) -> None:
        import batch_preflight
        PassCapTests.rebase_onto_moved_base(self)
        prs = '[{"number": 7, "headRefName": "fix/1208-x", "headRefOid": "new-head-sha"}]'
        with patch.object(hook, "_gh", return_value=self.pr_diff()), \
                patch.object(batch_preflight, "_gh", return_value=prs):
            self.assertIsNone(batch_preflight._stale_preclose_receipt(REPO, str(ISSUE)))

    def test_batch_preflight_at_the_cap_never_offers_a_third_pass(self) -> None:
        import batch_preflight
        self.commit("scripts/b.py")
        self.plan(tier="fast")
        self.complete()
        self.commit("scripts/c.py")
        prs = '[{"number": 7, "headRefName": "fix/1208-x", "headRefOid": "unreviewed-sha"}]'
        with patch.object(hook, "_gh", return_value=self.pr_diff()), \
                patch.object(batch_preflight, "_gh", return_value=prs):
            message = batch_preflight._stale_preclose_receipt(REPO, str(ISSUE))
        self.assertIn("WILL halt", message)
        self.assertIn("Escalate to the operator", message)
        self.assertNotIn("Re-run", message)

    def test_unreadable_pr_diff_fails_closed(self) -> None:
        with patch.object(hook, "_gh", return_value=None):
            self.assertFalse(hook._preclose_receipt_ok(REPO, str(ISSUE), "new-head-sha", "7"))

    def test_stale_message_below_the_cap_still_offers_a_pass(self) -> None:
        self.assertIn("Run the pre-close pass", hook._stale_receipt_message(REPO, str(ISSUE), "7", "abc"))

    def test_stale_message_at_the_cap_never_offers_a_third_pass(self) -> None:
        self.commit("scripts/b.py")
        self.plan(tier="fast")
        self.complete()
        message = hook._stale_receipt_message(REPO, str(ISSUE), "7", "abc")
        self.assertNotIn("Run the pre-close pass", message)
        self.assertIn("Escalate to the operator", message)


if __name__ == "__main__":
    unittest.main()
