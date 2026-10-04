#!/usr/bin/env python3
"""harmonic-forge#890 (reforged): each issue's preclose panel arm is decided
once and recorded as an enrollment event, read by plan and complete alike; the
experiment is on only by a declared flag; the by-arm report counts issues."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_preclose_check as base
import test_preclose_cost as cost_base

preclose = base.preclose
passes = preclose.preclose_passes
enrollment = preclose.preclose_enrollment
report = cost_base.report
REPO = "vitalharmony/hrse"


def issue_with(arm: str) -> int:
    return next(n for n in range(1, 500) if enrollment.hashed_arm(REPO, n) == arm)


class ArmCase(cost_base.CostCase):
    def setUp(self) -> None:
        super().setUp()
        self.issue = issue_with("workflow")
        self.enroll(True)

    def enroll(self, on: bool) -> None:
        enrollment.set_experiment(preclose.receipt_dir(), on, "test")

    def plan_arm(self, issue: int | None = None, **overrides) -> str:
        return self.plan(issue=issue or self.issue, **overrides)

    def complete_arm(self, issue: int | None = None, findings: list | None = None, **cost) -> None:
        args = base._Args(repo=REPO, issue=issue or self.issue, base="base", head="HEAD",
                          findings=self.findings_file([base.ANCHORED] if findings is None else findings),
                          envelope=None, not_triggered=True, cross_family=False, force=False,
                          own_model="claude-opus-5-5", tier="standard")
        args.__dict__.update(cost)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            preclose.complete(args)

    def receipt(self, issue: int | None = None) -> dict:
        return preclose.find_receipt(REPO, issue or self.issue) or {}

    def entry(self, issue: int | None = None) -> dict:
        return passes.history(self.receipt(issue))[-1]

    def decisions(self, issue: int | None = None) -> list[dict]:
        return enrollment.events(preclose.enrollment_path(REPO, issue or self.issue))

    def overridden_pass(self) -> None:
        self.plan_arm(arm="manual", arm_reason="no Workflow tool")
        self.complete_arm()
        self.commit("tools/y.py")


class TC1DecidedOnceAndRecorded(ArmCase):
    def test_the_same_issue_gets_the_same_arm_and_parities_differ(self) -> None:
        self.assertEqual(enrollment.hashed_arm(REPO, self.issue), "workflow")
        self.assertIn("arm:      workflow (assigned)", self.plan_arm())
        self.assertIn("arm:      manual (assigned)", self.plan_arm(issue_with("manual")))

    def test_plan_complete_plan_keeps_the_arm_and_records_one_decision(self) -> None:
        self.plan_arm()
        self.complete_arm()
        self.assertEqual(self.entry()["arm"], "workflow")
        self.commit("tools/y.py")
        with patch.object(enrollment, "hashed_arm", lambda repo, issue: "manual"):
            self.assertIn("arm:      workflow (assigned)", self.plan_arm())
        self.assertEqual(len(self.decisions()), 1)

    def test_a_receipt_rewrite_cannot_lose_the_decision(self) -> None:
        self.plan_arm()
        self.complete_arm()
        preclose.receipt_path(REPO, self.issue).write_text(json.dumps({"repo": REPO, "issue": self.issue}))
        self.commit("tools/y.py")
        with patch.object(enrollment, "hashed_arm", lambda repo, issue: "manual"):
            self.assertIn("arm:      workflow (assigned)", self.plan_arm())


class TC2ChangingTheArmIsAnEvent(ArmCase):
    def test_a_first_choice_off_the_assignment_needs_a_reason(self) -> None:
        with self.assertRaises(SystemExit) as refused:
            self.plan_arm(arm="manual")
        self.assertIn("assigned the workflow arm", str(refused.exception))
        self.assertEqual(self.decisions(), [])

    def test_an_override_survives_a_completion_and_auto_keeps_it(self) -> None:
        self.overridden_pass()
        self.assertTrue(self.entry()["arm_overridden"])
        self.assertEqual(self.entry()["arm_reason"], "no Workflow tool")
        self.assertIn("arm:      manual (overridden: no Workflow tool)", self.plan_arm())

    def test_a_plain_arm_flag_cannot_change_an_enrolled_issue(self) -> None:
        self.overridden_pass()
        with self.assertRaises(SystemExit) as refused:
            self.plan_arm(arm="workflow")
        self.assertIn("--re-enroll workflow", str(refused.exception))

    def test_rejoining_is_recorded_and_never_resurrected(self) -> None:
        self.overridden_pass()
        out = self.plan_arm(re_enroll="workflow", arm_reason="tool is back")
        self.assertIn("arm:      workflow (assigned)", out)
        # Pass 2's resurrection: a later default re-plan must keep the rejoin.
        self.assertIn("arm:      workflow (assigned)", self.plan_arm())
        self.assertEqual([d["arm"] for d in self.decisions()], ["manual", "workflow"])

    def test_a_multi_line_or_long_reason_is_refused(self) -> None:
        for reason in ("two\nlines", "x" * 201):
            with self.assertRaises(SystemExit):
                self.plan_arm(arm="manual", arm_reason=reason)


class TC3WorkflowInvocation(ArmCase):
    def test_the_plan_prints_the_invocation_with_lenses_and_resolved_shas(self) -> None:
        out = self.plan_arm(tier="standard")
        line = next(l for l in out.splitlines() if "Workflow name:" in l)
        args = json.loads(line.split("args: ", 1)[1])
        self.assertIn('name: "preclose-panel"', line)
        self.assertEqual(args["issue"], self.issue)
        self.assertEqual(args["lenses"], list(preclose.LENSES)[:3])
        for key in ("base", "head"):
            self.assertRegex(args[key], r"^[0-9a-f]{40}$")


class TC4ArmOnTheEntryAndEvent(ArmCase):
    def test_a_measured_pass_carries_the_arm_on_both(self) -> None:
        self.plan_arm()
        self.complete_arm()
        self.assertEqual(self.entry()["arm"], "workflow")
        attrs = self.events()[0]["attrs"]
        self.assertEqual((attrs["arm"], attrs["arm_overridden"]), ("workflow", False))

    def test_an_unavailable_cost_pass_carries_it_too(self) -> None:
        self.plan_arm()
        self.complete_arm(panel_tokens=None, panel_ms=None, cost_unavailable="no usage reported")
        self.assertEqual(self.entry()["arm"], "workflow")
        self.assertEqual(self.events()[0]["attrs"]["arm"], "workflow")

    def test_a_plan_less_completion_carries_no_arm(self) -> None:
        self.complete_arm()
        self.assertNotIn("arm", self.entry())
        self.assertNotIn("arm", self.events()[0]["attrs"])

    def test_arm_flags_outside_a_plan_are_refused(self) -> None:
        for flags in (["--arm", "manual", "--arm-reason", "x"], ["--re-enroll", "manual"]):
            done = subprocess.run([sys.executable, str(Path(preclose.__file__)), "--repo", REPO, "--issue", "1",
                                   "--complete", "--own-model", "m", *flags], capture_output=True, text=True)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn("belong to the plan", done.stderr)


def _write(receipts: Path, issue: int, entries: list[dict], **extra) -> None:
    (receipts / f"o_r_{issue}.json").write_text(json.dumps(
        {"repo": "o/r", "issue": issue, "pass_history": entries, **extra}))


def _pass(sha: str, arm: str | None = None, overridden: bool = False, **fields) -> dict:
    entry = {"sha": sha * 40, "epoch": 0, "surviving": 0, "raised": 2, "dismissed": 2,
             "panel_tokens": 100, "panel_ms": 10, "cross_family_ran": False, **fields}
    if arm:
        entry.update({"arm": arm, "arm_overridden": overridden})
    return entry


def _row(text: str, name: str) -> list[str]:
    line = next(l for l in text.splitlines() if l.startswith(f"| {name} |"))
    return [cell.strip() for cell in line.strip("|").split("|")]


class TC5IssueLevelBucketing(unittest.TestCase):
    def test_each_issue_lands_in_exactly_one_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipts = Path(tmp)
            _write(receipts, 1, [_pass("a", "manual", surviving=1), _pass("b", "manual")])
            _write(receipts, 2, [_pass("c", "workflow")])
            _write(receipts, 3, [_pass("d", "manual", overridden=True)])
            _write(receipts, 4, [_pass("e")])
            _write(receipts, 5, [_pass("f"), _pass("1", "workflow")])
            text = report.report(receipts, None, receipts / "no-archive", by_arm=True)
        manual = _row(text, "manual")
        self.assertEqual(manual[1:4], ["1", "2 / 2", "2.00 / 2.00"])
        self.assertEqual(manual[9], "1")  # needed pass 2: the two-pass issue is never split
        self.assertEqual(_row(text, "workflow")[1], "1")
        self.assertEqual(_row(text, "overridden")[1], "1")
        self.assertEqual(_row(text, "unarmed")[1], "1")
        self.assertEqual(_row(text, "mixed")[1:3], ["1", "2 / 2"])
        self.assertIn("n per arm: 1 manual / 1 workflow", text)
        self.assertIn("not yet meaningful below 8 issues per arm", text)


class TC6CodexAndTotalCost(unittest.TestCase):
    def test_the_codex_rate_median_and_total_cost_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipts = Path(tmp)
            _write(receipts, 1, [_pass("a", "manual", cross_family_ran=True, cross_family_ms=700),
                                 _pass("b", "manual")])
            _write(receipts, 2, [_pass("c", "manual", cross_family_ran=True, cross_family_ms=900,
                                       cross_family_fallback=True)])
            text = report.report(receipts, None, receipts / "no-archive", by_arm=True)
        manual = _row(text, "manual")
        # A fallback triggered the check: it counts in the rate (#890 pass 1).
        self.assertEqual(manual[11], "2/3, 1 fell back")
        self.assertEqual(manual[12], "800")
        self.assertEqual(manual[13], "300 panel tokens; 1,630 ms")
        self.assertIn("Codex tokens are not measured", text)


class TC7EnrollmentIsDeclared(ArmCase):
    def test_not_enrolling_records_pre_experiment_for_life(self) -> None:
        self.enroll(False)
        out = self.plan_arm()
        self.assertIn("arm:      pre-experiment (pre-experiment: the experiment is not enrolling)", out)
        with self.assertRaises(SystemExit) as refused:
            self.plan_arm(re_enroll="workflow", arm_reason="try it")
        self.assertIn("needs the experiment to be enrolling", str(refused.exception))
        self.complete_arm()
        self.enroll(True)
        self.commit("tools/y.py")
        self.assertIn("arm:      pre-experiment", self.plan_arm())
        self.assertEqual(self.entry()["arm"], enrollment.PRE_EXPERIMENT)

    def test_a_missing_or_corrupt_flag_is_not_enrolling(self) -> None:
        flag = preclose.receipt_dir() / enrollment.EXPERIMENT_FILE
        flag.write_text("{not json")
        self.assertFalse(enrollment.enrolling(preclose.receipt_dir()))
        flag.unlink()
        self.assertFalse(enrollment.enrolling(preclose.receipt_dir()))

    def test_an_enrolled_workflow_issue_keeps_its_arm_after_the_experiment_stops(self) -> None:
        self.plan_arm()
        self.enroll(False)
        (preclose.receipt_dir() / enrollment.EXPERIMENT_FILE).unlink()
        self.assertIn("arm:      workflow (assigned)", self.plan_arm())
        self.assertEqual(len(self.decisions()), 1)


class ReforgePass1Fixes(ArmCase):
    """harmonic-forge#890 reforged pass 1: one test per finding."""

    def test_manual_on_a_pre_experiment_issue_is_the_same_panel(self) -> None:
        self.enroll(False)
        self.plan_arm()
        self.assertIn("arm:      pre-experiment", self.plan_arm(arm="manual"))
        self.assertEqual(len(self.decisions()), 1)

    def test_an_unreadable_record_is_never_re_derived(self) -> None:
        path = preclose.enrollment_path(REPO, self.issue)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"arm": "workfl')
        with self.assertRaises(SystemExit) as refused:
            self.plan_arm()
        self.assertIn("never re-derived", str(refused.exception))
        self.assertEqual(path.read_text(), '{"arm": "workfl')

    def test_a_first_workflow_request_while_not_enrolling_says_plan_without_arm(self) -> None:
        self.enroll(False)
        with self.assertRaises(SystemExit) as refused:
            self.plan_arm(arm="workflow", arm_reason="try it")
        self.assertIn("no --arm", str(refused.exception))
        self.assertNotIn("--re-enroll", str(refused.exception))
        self.assertEqual(self.decisions(), [])

    def test_the_footer_counts_every_row_outside_the_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipts = Path(tmp)
            _write(receipts, 1, [_pass("a", "manual", overridden=True)])
            _write(receipts, 2, [_pass("b", "manual")])
            text = report.report(receipts, None, receipts / "no-archive", by_arm=True)
        self.assertIn("1 manual / 0 workflow; outside the comparison: 0 pre-experiment, 1 overridden", text)


class ReportUnit(unittest.TestCase):
    def test_kill_receipts_and_the_flag_are_not_issues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipts = Path(tmp)
            _write(receipts, 1, [_pass("a", "manual")])
            (receipts / "o_r_1.kill.json").write_text(json.dumps({"repo": "o/r", "issue": 1, "status": "pass"}))
            (receipts / enrollment.EXPERIMENT_FILE).write_text(json.dumps({"enrolling": True}))
            text = report.report(receipts, None, receipts / "no-archive", by_arm=True)
        self.assertEqual(_row(text, "manual")[1], "1")
        self.assertEqual(_row(text, "unarmed")[1], "0")

    def test_an_unmeasured_pass_is_never_free(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipts = Path(tmp)
            entry = _pass("a", "workflow")
            for key in ("panel_tokens", "panel_ms"):
                entry.pop(key)
            entry["cost_unavailable"] = "no usage"
            _write(receipts, 1, [entry])
            text = report.report(receipts, None, receipts / "no-archive", by_arm=True)
        workflow = _row(text, "workflow")
        self.assertIn("(0 of 1)", workflow[4])
        self.assertIn("1 pass(es) not measured", workflow[13])


class UnknownArmIsShown(unittest.TestCase):
    def test_an_unknown_arm_lands_in_other_not_a_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipts = Path(tmp)
            _write(receipts, 1, [_pass("a", "Workflow")])
            text = report.report(receipts, None, receipts / "no-archive", by_arm=True)
        self.assertEqual(_row(text, "other")[1], "1")


if __name__ == "__main__":
    unittest.main()
