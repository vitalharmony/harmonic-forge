#!/usr/bin/env python3
"""harmonic-forge#889: every completed preclose pass records what it cost,
emits one telemetry event, and the pass report shows it."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_preclose_check as base  # noqa: E402

preclose = base.preclose
import preclose_pass_report as report  # noqa: E402


class CostCase(base.ScratchRepo):
    def setUp(self) -> None:
        super().setUp()
        self.store = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.store, ignore_errors=True))
        patcher = patch.dict(os.environ, {"HARMONIC_FORGE_TELEMETRY_STORE": str(self.store)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.commit("tools/x.py")

    def envelope(self, fallback: bool = False) -> str:
        path = Path(self.findings_file([])).with_name("envelope.json")
        if fallback:
            path.write_text(json.dumps({"status": "process-error", "exit_code": 1, "label": base.FALLBACK}))
            return str(path)
        path.write_text(json.dumps({
            "status": "ok", "label": base.CROSS, "report": {"assumptions": [{"verdict": "confirmed"}]},
            "family": "codex", "posture": "verify", "exit_code": 0, "caller_family": "claude",
            "target_family": "codex",
            "native": [{"type": "thread.started"}, {"type": "item.completed", "item": {"type": "agent_message"}}]}))
        return str(path)

    def run_complete(self, findings: list | None = None, cross: bool = False, fallback: bool = False,
                     **cost) -> tuple[str, str]:
        """A pass with one surviving finding (not triggered) or a silent panel (cross-family)."""
        findings = [base.ANCHORED] if findings is None and not cross else (findings or [])
        args = base._Args(repo="vitalharmony/hrse", issue=1208, base="base", head="HEAD",
                          findings=self.findings_file(findings),
                          envelope=self.envelope(fallback) if cross else None, not_triggered=not cross,
                          cross_family=False, force=False, own_model="claude-opus-5-5", tier="standard")
        args.__dict__.update(cost)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            preclose.complete(args)
        return out.getvalue(), err.getvalue()

    def last_pass(self) -> dict:
        receipt = preclose.find_receipt("vitalharmony/hrse", 1208)
        return preclose.preclose_passes.history(receipt)[-1]

    def events(self) -> list[dict]:
        return [json.loads(line) for path in self.store.rglob("*.jsonl")
                for line in path.read_text().splitlines() if line.strip()]


class TC1CostIsRequired(CostCase):
    def test_a_pass_without_cost_is_refused_naming_what_is_missing(self) -> None:
        with self.assertRaises(SystemExit) as refused:
            self.run_complete(panel_tokens=None, panel_ms=None)
        self.assertIn("--panel-tokens and --panel-ms", str(refused.exception))
        with self.assertRaises(SystemExit) as refused:
            self.run_complete(panel_ms=None)
        self.assertIn("--panel-ms", str(refused.exception))
        self.assertNotIn("--panel-tokens", str(refused.exception).split("needs")[1].split(":")[0])
        self.assertIsNone(preclose.find_receipt("vitalharmony/hrse", 1208))

    def test_a_non_number_and_a_reasonless_escape_are_refused(self) -> None:
        with self.assertRaises(SystemExit) as refused:
            self.run_complete(panel_tokens="lots")
        self.assertIn("whole number", str(refused.exception))
        with self.assertRaises(SystemExit) as refused:
            self.run_complete(panel_tokens=None, panel_ms=None, cost_unavailable="  ")
        self.assertIn("reason", str(refused.exception))
        with self.assertRaises(SystemExit) as refused:
            self.run_complete(panel_tokens=None, panel_ms=None, cost_unavailable="x" * 201)
        self.assertIn("200 characters", str(refused.exception))


class TC2CostIsRecordedPerPass(CostCase):
    def test_the_pass_entry_carries_cost_and_counts(self) -> None:
        dismissed = {**base.ANCHORED, "anchor": "tools/x.py:2", "dismissed": "out of scope"}
        self.run_complete(findings=[base.ANCHORED, dismissed])
        entry = self.last_pass()
        self.assertEqual((entry["panel_tokens"], entry["panel_ms"]), (1000, 2000))
        self.assertEqual((entry["raised"], entry["surviving"], entry["dismissed"]), (2, 1, 1))
        self.assertIs(entry["cross_family_ran"], False)

    def test_an_unavailable_cost_records_the_reason_and_no_numbers(self) -> None:
        self.run_complete(panel_tokens=None, panel_ms=None, cost_unavailable="runtime reported no usage")
        entry = self.last_pass()
        self.assertEqual(entry["cost_unavailable"], "runtime reported no usage")
        self.assertNotIn("panel_tokens", entry)
        self.assertNotIn("panel_ms", entry)


class TC3OneEventPerPass(CostCase):
    def test_one_scalar_event_and_a_re_emit_is_a_duplicate(self) -> None:
        self.run_complete()
        events = [e for e in self.events() if e["event_type"] == "preclose.pass.completed"]
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual((event["source"], event["actor"]), ("advisory", "advisory:preclose-inspection"))
        attrs = event["attrs"]
        self.assertTrue(all(isinstance(v, (int, float, bool, str)) for v in attrs.values()), attrs)
        self.assertEqual((attrs["panel_tokens"], attrs["panel_ms"], attrs["pass"]), (1000, 2000, 1))
        self.assertNotIn("cost_unavailable", attrs)
        self.assertTrue(event["subject_id"].endswith("#p1e0"), event["subject_id"])
        # No free text: the only strings are the tier label.
        self.assertEqual({k for k, v in attrs.items() if isinstance(v, str)}, {"tier"})
        # Partitioned under the repo's real account and org, where #890's query looks.
        self.assertEqual((event["account"], event["org"]), ("vitalharmony", "vitalharmony"))
        self.assertFalse(any("unresolved" in p.parts for p in self.store.rglob("*.jsonl")))
        self.assertEqual(event["ts"], self.last_pass()["completed_at"])
        sha = preclose.find_receipt("vitalharmony/hrse", 1208)["reviewed_sha"]
        labels = {"pass": 1, "refuters": 0, "tier": "standard", "high_blast": False,
                  "cross_family_required": False}
        with contextlib.redirect_stderr(io.StringIO()):
            preclose.emit_pass("vitalharmony/hrse", 1208, sha, self.last_pass(), labels)
        self.assertEqual(len([e for e in self.events() if e["event_type"] == "preclose.pass.completed"]), 1)

    def test_the_event_is_stamped_with_the_pass_time_not_the_emit_time(self) -> None:
        self.run_complete()
        entry = {**self.last_pass(), "completed_at": "2026-01-02T03:04:05Z"}
        with contextlib.redirect_stderr(io.StringIO()):
            preclose.emit_pass("vitalharmony/hrse", 1208, "a" * 40, entry,
                               {"pass": 1, "refuters": 0, "tier": "standard", "high_blast": False,
                                "cross_family_required": False})
        self.assertIn("2026-01-02T03:04:05Z", {e["ts"] for e in self.events()})

    def test_the_event_carries_the_tier_the_pass_was_planned_at(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            self.plan(tier="deep")
        args = {"tier": None}
        self.run_complete(**args)
        self.assertEqual(self.events()[0]["attrs"]["tier"], "deep")

    def test_an_unavailable_cost_emits_no_invented_numbers(self) -> None:
        self.run_complete(panel_tokens=None, panel_ms=None, cost_unavailable="no usage")
        attrs = self.events()[0]["attrs"]
        self.assertIs(attrs["cost_unavailable"], True)
        self.assertNotIn("panel_tokens", attrs)


class TC4UnwritableStore(CostCase):
    def test_the_receipt_is_written_and_the_failure_is_on_stderr(self) -> None:
        blocker = self.store / "a-file"
        blocker.write_text("x")
        with patch.dict(os.environ, {"HARMONIC_FORGE_TELEMETRY_STORE": str(blocker / "store")}):
            out, err = self.run_complete()
        self.assertIn("recorded pass 1", out)
        self.assertEqual(self.last_pass()["panel_tokens"], 1000)
        self.assertIn("pass telemetry", err)


class TC5Report(unittest.TestCase):
    def test_tokens_and_ms_per_pass_with_a_median_and_total_footer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipts = Path(tmp)
            (receipts / "o_r_1.json").write_text(json.dumps({"repo": "o/r", "issue": 1, "pass_history": [
                {"sha": "a" * 40, "surviving": 1, "epoch": 0, "panel_tokens": 100, "panel_ms": 10,
                 "cross_family_ran": False},
                {"sha": "b" * 40, "surviving": 0, "epoch": 0, "panel_tokens": 300, "panel_ms": 30,
                 "cross_family_ran": True, "cross_family_ms": 700}]}))
            (receipts / "o_r_2.json").write_text(json.dumps({"repo": "o/r", "issue": 2, "pass_history": [
                {"sha": "c" * 40, "surviving": 0, "epoch": 0, "cost_unavailable": "no usage"},
                {"sha": "d" * 40, "surviving": 0, "epoch": 0}]}))
            text = report.report(receipts, None, receipts / "no-archive")
        self.assertIn("| o/r#1 | 1 | aaaaaaaaaaaa | 100 | 10 |  |", text)
        self.assertIn("| o/r#1 | 2 | bbbbbbbbbbbb | 300 | 30 | 700 |", text)
        self.assertIn("| o/r#2 | 1 | cccccccccccc | n/a | n/a |  |", text)
        self.assertIn("| o/r#2 | 2 | dddddddddddd | n/a | n/a |  |", text)
        self.assertIn("4 pass(es); panel tokens median 200, total 400 over 2 measured; "
                      "Codex check ran on 1/2 measured.", text)

    def test_a_reforged_issue_reports_every_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipts = Path(tmp)
            (receipts / "o_r_3.json").write_text(json.dumps({"repo": "o/r", "issue": 3, "pass_history": [
                {"sha": "e" * 40, "epoch": 0, "panel_tokens": 500, "panel_ms": 5, "cross_family_ran": True,
                 "cross_family_ms": 900},
                {"sha": "f" * 40, "epoch": 1, "panel_tokens": 100, "panel_ms": 1, "cross_family_ran": False}]}))
            text = report.report(receipts, None, receipts / "no-archive")
        self.assertIn("| o/r#3 | 1 | eeeeeeeeeeee | 500 | 5 | 900 |", text)
        self.assertIn("| o/r#3 | 1 (epoch 1) | ffffffffffff | 100 | 1 |  |", text)
        self.assertIn("total 600 over 2 measured; Codex check ran on 1/2 measured.", text)
        self.assertIn("orchestration cost", text)


class TC6CrossFamilyTime(CostCase):
    def test_an_envelope_without_its_time_is_refused(self) -> None:
        with self.assertRaises(SystemExit) as refused:
            self.run_complete(cross=True, cross_family_ms=None)
        self.assertIn("--cross-family-ms", str(refused.exception))

    def test_a_time_without_an_envelope_is_refused(self) -> None:
        with self.assertRaises(SystemExit) as refused:
            self.run_complete(cross_family_ms="5")
        self.assertIn("without --envelope", str(refused.exception))

    def test_a_fallback_call_still_records_its_time(self) -> None:
        self.run_complete(cross=True, fallback=True)
        entry = self.last_pass()
        self.assertEqual((entry["cross_family_ran"], entry["cross_family_ms"], entry["cross_family_fallback"]),
                         (True, 300, True))
        attrs = self.events()[0]["attrs"]
        self.assertEqual((attrs["cross_family_ms"], attrs["cross_family_fallback"]), (300, True))

    def test_the_entry_and_event_carry_the_codex_time(self) -> None:
        self.run_complete(cross=True)
        entry = self.last_pass()
        self.assertEqual((entry["cross_family_ran"], entry["cross_family_ms"], entry["cross_family_tokens"]),
                         (True, 300, "unavailable"))
        attrs = self.events()[0]["attrs"]
        self.assertEqual((attrs["cross_family_ran"], attrs["cross_family_ms"]), (True, 300))


class StickyWicketPatch(CostCase):
    """harmonic-forge#889 sticky-wicket PATCH: the pass entry is the single
    carrier, and every reader reads it through one accessor."""

    def test_the_tier_survives_a_re_plan_without_tier(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            self.plan(tier="deep")
        self.run_complete(tier=None)
        self.commit("tools/y.py")  # pass 2 reviews a new head
        with contextlib.redirect_stdout(io.StringIO()):
            self.plan(tier=None)
        self.run_complete(tier=None, findings=[{**base.ANCHORED, "anchor": "tools/y.py:1"}])
        events = sorted(self.events(), key=lambda e: e["attrs"]["pass"])
        self.assertEqual([e["attrs"]["tier"] for e in events], ["deep", "deep"])
        self.assertEqual(self.last_pass()["tier"], "deep")

    def test_a_re_plan_sizes_and_labels_at_the_same_tier(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            self.plan(tier="deep")
        self.run_complete(tier=None)
        self.commit("tools/z.py")
        self.assertIn("Tier deep", self.plan(tier=None))
        receipt = preclose.find_receipt("vitalharmony/hrse", 1208)
        self.assertEqual(receipt["tier"], "deep")

    def test_complete_cannot_relabel_the_tier_the_plan_sized(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            self.plan(tier="deep")
        self.run_complete(tier="fast")
        self.assertEqual(self.last_pass()["tier"], "deep")
        self.assertEqual(self.events()[0]["attrs"]["tier"], "deep")

    def test_a_completion_without_a_new_plan_keeps_the_last_tier(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            self.plan(tier="deep")
        self.run_complete(tier=None)
        self.commit("tools/w.py")  # a new head, completed without planning it
        self.run_complete(tier=None, force=True, findings=[{**base.ANCHORED, "anchor": "tools/w.py:1"}])
        self.assertEqual(self.last_pass()["tier"], "deep")

    def test_an_unset_plan_is_not_relabelled_at_complete(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            self.plan(tier=None)
        self.run_complete(tier="fast")
        self.assertEqual(self.last_pass()["tier"], "unset")

    def test_zero_is_not_a_measured_cost(self) -> None:
        for flags in ({"panel_tokens": "0"}, {"panel_ms": "0"}):
            with self.subTest(flags), self.assertRaises(SystemExit) as refused:
                self.run_complete(**flags)
            self.assertIn("--cost-unavailable", str(refused.exception))
        with self.assertRaises(SystemExit) as refused:
            self.run_complete(cross=True, cross_family_ms="0")
        self.assertIn("at least 1", str(refused.exception))

    def test_a_fallback_line_says_it_fell_back(self) -> None:
        out, _ = self.run_complete(cross=True, fallback=True)
        self.assertIn("fell back (no verdict)", out)


class ReportReadsOneMergedPassList(unittest.TestCase):
    def archive(self, root: Path, records: list[dict]) -> None:
        import gzip  # noqa: PLC0415
        folder = root / "o" / "preclose-receipts"
        folder.mkdir(parents=True)
        with gzip.open(folder / "2026-10.jsonl.gz", "wt", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps({"record": record}) + "\n")

    def test_archived_passes_keep_their_cost_and_the_counts_agree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipts, archive = root / "r", root / "a"
            receipts.mkdir()
            # An archived receipt whose pass 1 carried cost; the live receipt
            # holds only pass 2. Plus a pre-#834 archived head with no history.
            self.archive(archive, [
                {"repo": "o/r", "issue": 1, "status": "complete", "reviewed_sha": "a" * 40,
                 "pass_history": [{"sha": "a" * 40, "epoch": 0, "panel_tokens": 500, "panel_ms": 9,
                                   "cross_family_ran": False}]},
                {"repo": "o/r", "issue": 1, "status": "complete", "reviewed_sha": "c" * 40}])
            (receipts / "o_r_1.json").write_text(json.dumps({"repo": "o/r", "issue": 1, "pass_history": [
                {"sha": "b" * 40, "epoch": 0, "panel_tokens": 100, "panel_ms": 1, "cross_family_ran": False}]}))
            text = report.report(receipts, None, archive)
        self.assertIn("| o/r#1 | 3 |", text)  # the main table's pass count
        self.assertIn("| aaaaaaaaaaaa | 500 | 9 |", text)  # recovered, not n/a
        self.assertIn("| cccccccccccc | n/a | n/a |", text)
        self.assertIn("3 pass(es); panel tokens median 300, total 600 over 2 measured", text)

    def test_a_costed_archived_copy_wins_over_a_bare_one_in_either_order(self) -> None:
        bare = {"repo": "o/r", "issue": 4, "status": "complete", "reviewed_sha": "f" * 40,
                "pass_history": [{"sha": "f" * 40, "epoch": 0}]}
        costed = {**bare, "pass_history": [{"sha": "f" * 40, "epoch": 0, "panel_tokens": 70, "panel_ms": 7,
                                            "cross_family_ran": False}]}
        for order in ((bare, costed), (costed, bare)):
            with self.subTest(first=order[0] is bare), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "r").mkdir()
                self.archive(root / "a", list(order))
                (root / "r" / "o_r_4.json").write_text(json.dumps({"repo": "o/r", "issue": 4, "pass_history": []}))
                text = report.report(root / "r", None, root / "a")
                self.assertIn("| ffffffffffff | 70 | 7 |", text)

    def test_a_fallback_is_shown_but_not_counted_as_a_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            receipts = Path(tmp)
            (receipts / "o_r_2.json").write_text(json.dumps({"repo": "o/r", "issue": 2, "pass_history": [
                {"sha": "d" * 40, "epoch": 0, "panel_tokens": 10, "panel_ms": 1, "cross_family_ran": True,
                 "cross_family_ms": 180000, "cross_family_fallback": True},
                {"sha": "e" * 40, "epoch": 0, "panel_tokens": 10, "panel_ms": 1, "cross_family_ran": True,
                 "cross_family_ms": 900}]}))
            text = report.report(receipts, None, receipts / "none")
        self.assertIn("| 180,000 (fell back) |", text)
        self.assertIn("Codex check ran on 1/1 measured, 1 fell back without a verdict.", text)


if __name__ == "__main__":
    unittest.main()
