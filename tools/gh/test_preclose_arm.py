#!/usr/bin/env python3
"""harmonic-forge#890: each issue's preclose panel arm is assigned
deterministically, persists through every receipt rewrite, rides on the pass
entry and its event, and the by-arm report buckets issues, never entries."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_preclose_check as base
import test_preclose_cost as cost_base

preclose = base.preclose
passes = preclose.preclose_passes
report = cost_base.report
REPO = "vitalharmony/hrse"


def issue_with(arm: str) -> int:
    return next(n for n in range(1, 500) if passes.hashed_arm(REPO, n) == arm)


class ArmCase(cost_base.CostCase):
    def setUp(self) -> None:
        super().setUp()
        self.workflow = Path(tempfile.mkdtemp()) / "preclose-panel.js"
        self.workflow.write_text("// stand-in for harmonic-forge#891's workflow\n")
        patcher = patch.object(preclose, "PRECLOSE_PANEL_WORKFLOW", self.workflow)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.issue = issue_with("workflow")

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


class TC1DeterministicAndPersisted(ArmCase):
    def test_the_same_issue_gets_the_same_arm_and_parities_differ(self) -> None:
        self.assertEqual(passes.hashed_arm(REPO, self.issue), passes.hashed_arm(REPO, self.issue))
        self.assertIn("arm:      workflow (assigned)", self.plan_arm())
        manual = issue_with("manual")
        self.assertIn("arm:      manual (assigned)", self.plan_arm(manual))

    def test_plan_complete_plan_keeps_the_arm_even_if_the_hash_moves(self) -> None:
        self.plan_arm()
        self.complete_arm()
        self.assertEqual(self.entry()["arm"], "workflow")
        self.commit("tools/y.py")
        with patch.object(passes, "hashed_arm", lambda repo, issue: "manual"):
            self.assertIn("arm:      workflow (assigned)", self.plan_arm())
        self.assertEqual(self.receipt()["panel_arm"], "workflow")

    def test_the_arm_survives_from_the_entry_alone(self) -> None:
        # A receipt whose top-level fields were lost keeps the entry's arm.
        self.plan_arm()
        self.complete_arm()
        path = Path(preclose.receipt_dir()) / f"{REPO.replace('/', '_')}_{self.issue}.json"
        receipt = json.loads(path.read_text())
        receipt.pop("panel_arm")
        path.write_text(json.dumps(receipt))
        self.commit("tools/y.py")
        with patch.object(passes, "hashed_arm", lambda repo, issue: "manual"):
            self.assertIn("arm:      workflow (assigned)", self.plan_arm())


class TC2OverrideNeedsAReason(ArmCase):
    def test_no_reason_refuses_naming_the_assigned_arm(self) -> None:
        with self.assertRaises(SystemExit) as refused:
            self.plan_arm(arm="manual")
        self.assertIn("assigned the workflow arm", str(refused.exception))
        self.assertIsNone(preclose.find_receipt(REPO, self.issue))

    def test_a_reason_is_recorded_and_survives_a_completion(self) -> None:
        out = self.plan_arm(arm="manual", arm_reason="no Workflow tool in this runtime")
        self.assertIn("arm:      manual (overridden: no Workflow tool in this runtime)", out)
        self.assertNotIn("Workflow name:", out)
        self.complete_arm()
        self.assertEqual(self.entry()["arm"], "manual")
        self.assertTrue(self.entry()["arm_overridden"])
        self.assertEqual(self.receipt()["panel_arm_override"]["reason"], "no Workflow tool in this runtime")
        self.commit("tools/y.py")
        self.assertIn("arm:      manual (overridden:", self.plan_arm())

    def test_a_multi_line_or_long_reason_is_refused(self) -> None:
        for reason in ("two\nlines", "x" * 201):
            with self.assertRaises(SystemExit):
                self.plan_arm(arm="manual", arm_reason=reason)


class TC3WorkflowInvocation(ArmCase):
    def test_the_plan_prints_the_invocation_with_its_lenses(self) -> None:
        out = self.plan_arm(tier="standard")
        line = next(l for l in out.splitlines() if "Workflow name:" in l)
        args = json.loads(line.split("args: ", 1)[1])
        self.assertIn('name: "preclose-panel"', line)
        self.assertEqual(args["issue"], self.issue)
        self.assertEqual(args["lenses"], list(preclose.LENSES)[:3])


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
        self.assertEqual(manual[11], "1/2, 1 fell back")
        self.assertEqual(manual[12], "800")
        self.assertEqual(manual[13], "300 panel tokens; 1,630 ms")
        self.assertIn("Codex tokens are not measured", text)


class TC7PreExperiment(ArmCase):
    def test_without_the_workflow_auto_records_pre_experiment_and_it_persists(self) -> None:
        self.workflow.unlink()
        out = self.plan_arm()
        self.assertIn("arm:      pre-experiment (pre-experiment: harmonic-forge#891 not landed)", out)
        self.assertNotIn("panel_arm_override", self.receipt())
        with self.assertRaises(SystemExit) as refused:
            self.plan_arm(arm="workflow", arm_reason="try it")
        self.assertIn("harmonic-forge#891", str(refused.exception))
        self.plan_arm(arm="manual")  # the manual panel IS the pre-experiment panel: no override
        self.complete_arm()
        self.workflow.write_text("// landed\n")
        self.commit("tools/y.py")
        self.assertIn("arm:      pre-experiment", self.plan_arm())
        self.assertEqual(self.entry()["arm"], passes.PRE_EXPERIMENT)


if __name__ == "__main__":
    unittest.main()
