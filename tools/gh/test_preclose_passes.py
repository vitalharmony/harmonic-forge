#!/usr/bin/env python3
"""harmonic-forge#834 AC7: the per-issue preclose pass cap, and (harmonic-forge
#1077) change-identity acceptance: exact head SHA or a clean merge-tree replay,
never patch-id.

Runs the real `preclose_check` plan/complete against a scratch git repo
(`ScratchRepo`, from `test_preclose_check.py`) and the real merge hook's
receipt check, so the two enforcement points are proven to agree on one
receipt shape rather than each against its own mock.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
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

    def test_receipt_records_count_survivors_and_change_identity(self) -> None:
        self.run_pass("scripts/a.py", SURVIVOR)
        stored = self.receipt()
        self.assertEqual(stored["pass_count"], 1)
        self.assertEqual(stored["surviving_findings"], 1)
        identity = preclose_passes.change_identity(None, "HEAD", "base")
        self.assertEqual(stored["reviewed_change_identity"], identity)
        self.assertEqual(stored["pass_history"][-1]["change_identity"], identity)
        self.assertNotIn("reviewed_patch_id", stored)

    def test_planning_a_second_pass_does_not_reset_the_count(self) -> None:
        self.run_pass("scripts/a.py", SURVIVOR)
        self.commit("scripts/b.py")
        self.plan(tier="fast")
        self.assertEqual(self.receipt()["status"], "planned")
        self.assertEqual(self.receipt()["pass_count"], 1)

    def test_clean_rebase_is_not_a_new_pass(self) -> None:
        self.run_pass("scripts/a.py")
        self.rebase_onto_moved_base()
        with self.assertRaises(SystemExit) as caught:
            self.plan(tier="fast")
        self.assertIn("clean replay", str(caught.exception))
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
                "--envelope", self.envelope(label), "--own-model", "claude-opus-5-5",
                "--cross-family-ms", "300"]
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

    def test_an_unresolved_cluster_refusal_prints_a_runnable_command(self) -> None:
        """harmonic-forge#852 pass 2: the refusal path (every attempt after the
        cluster pass) prints the cluster route with the real repo and issue,
        never placeholders, and that command parses as printed."""
        self.run_pass("scripts/a.py", self.findings("same mechanism", "same mechanism"))
        self.commit("scripts/b.py")
        with self.assertRaises(SystemExit) as refused:
            self.plan(tier="fast")
        text = str(refused.exception)
        self.assertIn(f"--repo {REPO} --issue {ISSUE} --cluster-verdict PATCH", text)
        self.assertNotIn("<owner/repo>", text)
        self.assertNotIn("<N>", text)

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
            main_ref="base", force=True, repo=REPO, issue=ISSUE))
        # harmonic-forge#1169: --force --reforge takes no grant. The refused
        # plain forces above left one on disk; remove it so this proves that.
        for grant in (Path(os.environ["HOME"]) / ".claude/state/force").glob("*.json"):
            grant.unlink()
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
        reforge_args = _Args(repo=f"https://github.com/{REPO}.git", issue=ISSUE,
                             cluster_verdict="REFORGE",
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
        last = preclose_passes.reviewed(preclose.find_receipt(REPO, ISSUE))
        head = preclose.run("git", "rev-parse", "HEAD").stdout.strip()
        self.assertFalse(preclose_passes.accepts_entry(last, head, None, "base"))
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


class ChangeIdentityTests(ScratchRepo):
    """harmonic-forge#1077 AC1-AC4: the shared primitive, against real git.
    `base` plays origin/main; the checked-out `main` branch is the PR."""

    PAD = "# --\n" * 4
    BLOCK = PAD + "def f():\n    return 1\n" + PAD

    def head(self) -> str:
        return preclose.run("git", "rev-parse", "HEAD").stdout.strip()

    def identity(self) -> dict:
        return preclose_passes.change_identity(None, "HEAD", "base")

    def accepts(self, recorded: dict) -> bool:
        return preclose_passes.accepts(recorded, self.head(), None, None, "base")

    def on_base(self, relpath: str, body: str) -> None:
        """Main moves: commit `relpath` on `base`, then return to the PR."""
        git("checkout", "-q", "base", cwd=self.repo)
        self.commit(relpath, body)
        git("checkout", "-q", "main", cwd=self.repo)

    def seed_base(self, files: dict[str, str]) -> None:
        """Start main (and the PR) from these files."""
        for relpath, body in files.items():
            target = self.repo / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body)
        git("add", "-A", cwd=self.repo)
        git("commit", "-qm", "seed files", cwd=self.repo)
        git("branch", "-f", "base", "HEAD", cwd=self.repo)

    def test_change_identity_is_local_git_and_never_gh(self) -> None:
        """AC1."""
        self.commit("scripts/a.py")
        calls: list[list[str]] = []
        real = subprocess.run

        def recording(argv, *args, **kwargs):
            calls.append(list(argv))
            return real(argv, *args, **kwargs)

        with patch.object(preclose_passes.subprocess, "run", side_effect=recording):
            identity = self.identity()
        self.assertEqual(identity, {
            "head_sha": self.head(),
            "merge_base_sha": preclose.run("git", "merge-base", "base", "HEAD").stdout.strip(),
            "tree": preclose.run("git", "rev-parse", "HEAD^{tree}").stdout.strip()})
        self.assertTrue(calls)
        self.assertTrue(all(call[0] == "git" for call in calls), calls)

    def test_exact_head_is_accepted(self) -> None:
        """(a)."""
        self.commit("scripts/a.py")
        self.assertTrue(self.accepts(self.identity()))

    def test_edit_to_a_second_identical_block_is_refused(self) -> None:
        """AC2: the hole patch-id left open. Same patch-id, different tree."""
        self.seed_base({"lib.py": self.BLOCK + self.BLOCK})
        self.commit("lib.py", self.BLOCK.replace("1", "2") + self.BLOCK)
        reviewed, reviewed_patch = self.identity(), preclose.local_patch_id("base", "HEAD")
        git("reset", "-q", "--hard", "base", cwd=self.repo)
        self.commit("lib.py", self.BLOCK + self.BLOCK.replace("1", "2"))
        self.assertEqual(preclose.local_patch_id("base", "HEAD"), reviewed_patch)
        self.assertFalse(self.accepts(reviewed))

    def test_clean_rebase_onto_moved_main_is_accepted(self) -> None:
        """AC3."""
        self.commit("scripts/a.py")
        reviewed = self.identity()
        self.on_base("unrelated.txt", "u\n")
        git("rebase", "-q", "base", cwd=self.repo)
        self.assertNotEqual(self.head(), reviewed["head_sha"])
        self.assertTrue(self.accepts(reviewed))

    def test_main_moved_but_head_not_rebased_is_accepted(self) -> None:
        """AC3: cur_base is merge-base(main, head), never main's tip."""
        self.commit("scripts/a.py")
        reviewed = self.identity()
        git("commit", "-q", "--amend", "-m", "reworded", cwd=self.repo)  # new SHA, same tree
        self.on_base("unrelated.txt", "u\n")
        self.assertNotEqual(self.head(), reviewed["head_sha"])
        self.assertTrue(self.accepts(reviewed))
        tip = preclose.run("git", "rev-parse", "base").stdout.strip()
        self.assertFalse(preclose_passes.accepts(reviewed, self.head(), tip, None, "base"))

    def test_conflicting_replay_is_refused(self) -> None:
        """AC3: a replay that conflicts is refused, however the head resolved it."""
        self.seed_base({"lib.py": "a\nb\nc\n"})
        self.commit("lib.py", "a\nB-feature\nc\n")
        reviewed = self.identity()
        self.on_base("lib.py", "a\nB-main\nc\n")
        git("reset", "-q", "--hard", "base", cwd=self.repo)
        self.commit("lib.py", "a\nB-feature\nc\n")  # the conflict, resolved to the PR side
        self.assertFalse(self.accepts(reviewed))

    def test_head_committing_the_conflict_marker_tree_is_refused(self) -> None:
        """AC3, preclose pass 1: the head commits merge-tree's conflicted
        tree verbatim, so the tree comparison matches and only the replay's
        nonzero exit refuses it."""
        self.seed_base({"lib.py": "a\nb\nc\n"})
        self.commit("lib.py", "a\nB-feature\nc\n")
        reviewed = self.identity()
        self.on_base("lib.py", "a\nB-main\nc\n")
        # By SHA, as accepts() passes it: the markers embed the ref names.
        base_sha = preclose.run("git", "rev-parse", "base").stdout.strip()
        replay = subprocess.run(
            ["git", "merge-tree", "--write-tree", "--merge-base",
             reviewed["merge_base_sha"], base_sha, reviewed["head_sha"]],
            cwd=self.repo, capture_output=True, text=True, check=False)
        self.assertNotEqual(replay.returncode, 0)
        marker_tree = replay.stdout.split()[0]
        head = preclose.run("git", "commit-tree", marker_tree, "-p", "base",
                            "-m", "markers").stdout.strip()
        git("reset", "-q", "--hard", head, cwd=self.repo)
        self.assertIn("<<<<<<<", (self.repo / "lib.py").read_text())
        self.assertEqual(preclose.run("git", "rev-parse", "HEAD^{tree}").stdout.strip(),
                         marker_tree)
        self.assertFalse(self.accepts(reviewed))

    def test_extra_change_after_a_rebase_is_refused(self) -> None:
        """AC3: any non-exempt tree difference."""
        self.commit("scripts/a.py")
        reviewed = self.identity()
        self.on_base("unrelated.txt", "u\n")
        git("rebase", "-q", "base", cwd=self.repo)
        self.commit("scripts/extra.py")
        self.assertFalse(self.accepts(reviewed))

    PACKAGE = {"name": "x", "version": "1.0.0", "dependencies": {"a": {"version": "1"}}}
    LOCK = {"name": "x", "version": "1.0.0", "lockfileVersion": 3,
            "packages": {"": {"name": "x", "version": "1.0.0"},
                         "node_modules/a": {"version": "1"}}}

    def reviewed_then_rebased(self, package: dict | None = None) -> dict:
        package = self.PACKAGE if package is None else package
        self.seed_base({"frontend/package.json": json.dumps(package, indent=2) + "\n",
                        "frontend/package-lock.json": json.dumps(self.LOCK, indent=2) + "\n",
                        "other.json": json.dumps({"version": "1.0.0"}) + "\n"})
        self.commit("scripts/a.py")
        reviewed = self.identity()
        self.on_base("unrelated.txt", "u\n")
        git("rebase", "-q", "base", cwd=self.repo)
        return reviewed

    def bump(self, relpath: str, document: dict) -> None:
        self.commit(relpath, json.dumps(document, indent=2) + "\n")

    def test_top_level_version_bumps_are_exempt(self) -> None:
        """AC4: package.json version, package-lock.json version and packages[""].version."""
        reviewed = self.reviewed_then_rebased()
        self.bump("frontend/package.json", {**self.PACKAGE, "version": "1.0.1"})
        lock = json.loads(json.dumps(self.LOCK))
        lock["version"] = lock["packages"][""]["version"] = "1.0.1"
        self.bump("frontend/package-lock.json", lock)
        self.assertTrue(self.accepts(reviewed))

    def test_nested_version_change_is_refused(self) -> None:
        """AC4: only the listed keys; a nested "version" is a real change."""
        reviewed = self.reviewed_then_rebased()
        package = json.loads(json.dumps(self.PACKAGE))
        package["dependencies"]["a"]["version"] = "2"
        self.bump("frontend/package.json", package)
        self.assertFalse(self.accepts(reviewed))

    def test_nested_lockfile_version_change_is_refused(self) -> None:
        reviewed = self.reviewed_then_rebased()
        lock = json.loads(json.dumps(self.LOCK))
        lock["packages"]["node_modules/a"]["version"] = "2"
        self.bump("frontend/package-lock.json", lock)
        self.assertFalse(self.accepts(reviewed))

    def test_symlinked_package_json_is_never_exempt(self) -> None:
        """F1036 epoch 2 survivor 3: a symlink's blob is its link text, so a
        retargeted package.json symlink is refused, never parsed as JSON."""
        self.seed_base({"README": "r\n"})
        link = self.repo / "frontend" / "package.json"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to('{"version":"1.0.0"}')
        git("add", "-A", cwd=self.repo)
        git("commit", "-qm", "link", cwd=self.repo)
        git("branch", "-f", "base", "HEAD", cwd=self.repo)
        self.commit("scripts/a.py")
        reviewed = self.identity()
        self.on_base("unrelated.txt", "u\n")
        git("rebase", "-q", "base", cwd=self.repo)
        link.unlink()
        link.symlink_to('{"version":"1.0.1"}')
        git("add", "-A", cwd=self.repo)
        git("commit", "-qm", "retarget", cwd=self.repo)
        self.assertFalse(self.accepts(reviewed))

    def test_version_in_another_json_file_is_refused(self) -> None:
        reviewed = self.reviewed_then_rebased()
        self.bump("other.json", {"version": "1.0.1"})
        self.assertFalse(self.accepts(reviewed))

    def test_a_version_bump_beside_another_package_change_is_refused(self) -> None:
        reviewed = self.reviewed_then_rebased()
        self.bump("frontend/package.json", {**self.PACKAGE, "version": "1.0.1", "private": True})
        self.assertFalse(self.accepts(reviewed))

    TYPED = {**PACKAGE, "private": True, "weight": 1, "ratio": 1.5, "files": ["a", "b"]}

    def test_json_exemption_is_type_strict(self) -> None:
        """Preclose pass 1: Python == equates true, 1 and 1.0; the exemption
        must not. Key sets and list order are exact too."""
        for change in ({"private": 1}, {"weight": 1.0}, {"weight": True},
                       {"files": ["b", "a"]}, {"extra": None}):
            with self.subTest(change=change):
                self.setUp()
                reviewed = self.reviewed_then_rebased(self.TYPED)
                self.bump("frontend/package.json", {**self.TYPED, "version": "1.0.1", **change})
                self.assertFalse(self.accepts(reviewed))

    def test_json_exemption_compares_numbers_exactly(self) -> None:
        """F1036 epoch 2 survivor 3: 1.5 and 1.5000000000000001 parse to the
        same float; they must not be the same JSON value."""
        reviewed = self.reviewed_then_rebased(self.TYPED)
        text = json.dumps({**self.TYPED, "version": "1.0.1"}, indent=2) + "\n"
        self.commit("frontend/package.json", text.replace("1.5", "1.5000000000000001"))
        self.assertFalse(self.accepts(reviewed))

    def test_json_exemption_still_accepts_a_typed_document(self) -> None:
        reviewed = self.reviewed_then_rebased(self.TYPED)
        self.bump("frontend/package.json", {**self.TYPED, "version": "1.0.1"})
        self.assertTrue(self.accepts(reviewed))

    def test_accepts_returns_false_on_every_error(self) -> None:
        """accepts never raises, so the preflight and the merge hook agree."""
        self.commit("scripts/a.py")
        missing = {"head_sha": "f" * 40, "merge_base_sha": "e" * 40, "tree": "d" * 40}
        self.assertFalse(preclose_passes.accepts(missing, self.head(), None, None, "base"))
        self.assertFalse(preclose_passes.accepts(self.identity(), "not-a-sha", None, None, "base"))
        self.assertFalse(preclose_passes.accepts(missing, self.head(), None,
                                                 "/nonexistent/repo", "base"))
        self.assertFalse(preclose_passes.accepts(missing, self.head(), None,
                                                 lambda: None, "base"))

    def test_legacy_entry_is_accepted_at_its_exact_sha_only(self) -> None:
        """AC6: a pass recorded before #1077 carries a patch-id, not an identity."""
        self.commit("scripts/a.py")
        legacy = {"sha": self.head(), "patch_id": preclose.local_patch_id("base", "HEAD")}
        self.assertTrue(preclose_passes.accepts_entry(legacy, self.head(), None, "base"))
        self.on_base("unrelated.txt", "u\n")
        git("rebase", "-q", "base", cwd=self.repo)
        self.assertEqual(preclose.local_patch_id("base", "HEAD"), legacy["patch_id"])
        self.assertFalse(preclose_passes.accepts_entry(legacy, self.head(), None, "base"))
        self.assertIn("harmonic-forge#1077", preclose_passes.legacy_message(legacy, self.head()))


class HookChangeIdentityTests(ScratchRepo):
    """AC2/AC3/AC6 through the real hook, the real receipt and a real cache
    clone fetched from a local stand-in for GitHub (no network)."""

    PR = "7"

    def setUp(self) -> None:
        super().setUp()
        self.remote = Path(tempfile.mkdtemp()) / "github.git"
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.remote.parent)]))
        git("init", "-q", "--bare", str(self.remote), cwd=self.repo)
        # GitHub serves any commit in the repository by SHA; so does this.
        git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=self.remote)
        remote = patch.object(hook, "_cache_remote_url", return_value=str(self.remote))
        remote.start()
        self.addCleanup(remote.stop)
        self.commit("scripts/a.py")
        self.plan(tier="fast")
        self.complete()
        self.reviewed = preclose.find_receipt(REPO, ISSUE)["reviewed_sha"]
        self.publish()

    def head(self) -> str:
        return preclose.run("git", "rev-parse", "HEAD").stdout.strip()

    def publish(self) -> None:
        """What GitHub would show: `base` as main, HEAD as the PR head."""
        git("push", "-q", "--force", str(self.remote), "base:refs/heads/main",
            f"HEAD:refs/pull/{self.PR}/head", cwd=self.repo)

    def ok(self, head: str | None = None) -> bool:
        return hook._preclose_receipt_ok(REPO, str(ISSUE), head or self.head(), self.PR)

    def preflight(self) -> str | None:
        import batch_preflight
        prs = json.dumps([{"number": int(self.PR), "headRefName": f"fix/{ISSUE}-x",
                           "headRefOid": self.head()}])
        with patch.object(batch_preflight, "_gh", return_value=prs):
            return batch_preflight._stale_preclose_receipt(REPO, str(ISSUE))

    def test_clean_rebase_onto_moved_main_is_accepted(self) -> None:
        PassCapTests.rebase_onto_moved_base(self)
        self.publish()
        self.assertNotEqual(self.head(), self.reviewed)
        self.assertTrue(self.ok())
        self.assertIsNone(self.preflight())

    def test_exact_head_needs_no_fetch(self) -> None:
        """(a) is decided before any fetch: an unreachable remote still accepts it."""
        with patch.object(hook, "_cache_remote_url", return_value="/nonexistent/remote.git"):
            self.assertTrue(self.ok(self.reviewed))

    def test_second_identical_block_edit_is_refused(self) -> None:
        """AC2, end to end: the reviewed head edits block 1; the PR now edits block 2."""
        block = ChangeIdentityTests.BLOCK
        git("reset", "-q", "--hard", "base", cwd=self.repo)
        self.commit("lib.py", block + block)
        git("branch", "-f", "base", "HEAD", cwd=self.repo)
        self.commit("lib.py", block.replace("1", "2") + block)
        self.plan(tier="fast")
        self.complete()
        reviewed_patch = preclose.local_patch_id("base", "HEAD")
        git("reset", "-q", "--hard", "base", cwd=self.repo)
        self.commit("lib.py", block + block.replace("1", "2"))
        self.assertEqual(preclose.local_patch_id("base", "HEAD"), reviewed_patch)
        self.publish()
        self.assertFalse(self.ok())

    def test_real_change_is_denied(self) -> None:
        self.commit("scripts/extra.py")
        self.publish()
        self.assertFalse(self.ok())

    def test_fetch_failure_denies_and_preflight_agrees(self) -> None:
        PassCapTests.rebase_onto_moved_base(self)
        self.publish()
        with patch.object(hook, "_cache_remote_url", return_value="/nonexistent/remote.git"):
            self.assertFalse(self.ok())
            self.assertIn("WILL halt", self.preflight())

    def test_fetch_timeout_denies(self) -> None:
        PassCapTests.rebase_onto_moved_base(self)
        self.publish()
        with patch.object(hook, "FETCH_BUDGET_SECONDS", 0):
            self.assertFalse(self.ok())

    def test_pr_head_that_moved_after_the_read_denies(self) -> None:
        """The fetched PR head must be the head the hook was asked about."""
        PassCapTests.rebase_onto_moved_base(self)
        rebased = self.head()
        self.commit("scripts/later.py")
        self.publish()
        self.assertFalse(self.ok(rebased))

    def test_legacy_receipt_exact_sha_accepted_rebased_denied_naming_1077(self) -> None:
        """AC6: a patch-id-only receipt is never accepted by patch-id any more."""
        receipt = preclose.find_receipt(REPO, ISSUE)
        legacy_pass = {"sha": self.reviewed, "patch_id": preclose.local_patch_id("base", "HEAD"),
                       "surviving": 0, "epoch": 0}
        receipt["pass_history"] = [legacy_pass]
        receipt.pop("reviewed_change_identity", None)
        preclose.write_receipt(REPO, ISSUE, self.reviewed, receipt["refuters"], "complete",
                               extra=receipt)
        self.assertTrue(self.ok(self.reviewed))
        PassCapTests.rebase_onto_moved_base(self)
        self.publish()
        self.assertEqual(preclose.local_patch_id("base", "HEAD"), legacy_pass["patch_id"])
        self.assertFalse(self.ok())
        message = hook._stale_receipt_message(REPO, str(ISSUE), self.PR, self.head())
        self.assertIn("harmonic-forge#1077", message)
        self.assertIn("re-review", message)

    def test_batch_preflight_at_the_cap_never_offers_a_third_pass(self) -> None:
        self.commit("scripts/b.py")
        self.plan(tier="fast")
        self.complete()
        self.commit("scripts/c.py")
        self.publish()
        message = self.preflight()
        self.assertIn("WILL halt", message)
        self.assertIn("Escalate to the operator", message)
        self.assertNotIn("Re-run", message)

    def test_stale_message_below_the_cap_still_offers_a_pass(self) -> None:
        self.assertIn("Run the pre-close pass",
                      hook._stale_receipt_message(REPO, str(ISSUE), self.PR, "abc"))

    def test_stale_message_at_the_cap_never_offers_a_third_pass(self) -> None:
        self.commit("scripts/b.py")
        self.plan(tier="fast")
        self.complete()
        message = hook._stale_receipt_message(REPO, str(ISSUE), self.PR, "abc")
        self.assertNotIn("Run the pre-close pass", message)
        self.assertIn("Escalate to the operator", message)


if __name__ == "__main__":
    unittest.main()
