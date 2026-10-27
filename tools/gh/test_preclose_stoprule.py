#!/usr/bin/env python3
"""The stop rule, recorded in shadow (harmonic-forge#1189)."""
import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import preclose_passes as passes  # noqa: E402
import preclose_pass_report as report  # noqa: E402
from test_preclose_check import CROSS, ScratchRepo, _Args, preclose  # noqa: E402

FINDING = {"anchor": "tools/x.py:1", "scenario": "s", "mechanism": "m"}


def finding(repro=None, threat=None, anchor="tools/x.py:1"):
    out = {**FINDING, "anchor": anchor}
    if repro is not None:
        out["repro"] = repro
    if threat is not None:
        out["threat"] = threat
    return out


REPRODUCED = {"command": "t", "reproduced": True, "exit_status": 1, "ran_at_sha": "a"}
NOT_REPRODUCED = {"command": "t", "reproduced": False, "exit_status": 1, "ran_at_sha": "a"}


class DecisionTests(unittest.TestCase):  # AC1, AC6
    def decide(self, findings, model="good-faith", prior=(), reverify=None, head="h"):
        return passes.stop_rule_shadow(findings, model, list(prior), reverify, head)["decision"]

    def test_missing_repro_is_unknown(self):
        self.assertEqual(self.decide([finding(threat="good-faith")]), "UNKNOWN")

    def test_malformed_repro_is_unknown(self):
        for repro in ({"reproduced": "yes", "exit_status": 1}, {"reproduced": True},
                      {"reproduced": False, "exit_status": True}, "ran it"):
            with self.subTest(repro=repro):
                self.assertEqual(self.decide([finding(repro, "good-faith")]), "UNKNOWN")

    def test_not_reproduced_everywhere_accepts(self):
        self.assertEqual(self.decide([finding(NOT_REPRODUCED, "good-faith")]), "ACCEPT")
        self.assertEqual(self.decide([]), "ACCEPT")

    def test_reproduced_within_model_continues(self):
        self.assertEqual(self.decide([finding(REPRODUCED, "good-faith"),
                                      finding(NOT_REPRODUCED, "good-faith", "b:2")]), "CONTINUE")

    def test_exit0_expected_failure_repro_is_blocking(self):
        """`reproduced` decides; an exit status of 0 says nothing about it."""
        repro = {**REPRODUCED, "exit_status": 0}
        self.assertEqual(self.decide([finding(repro, "good-faith")]), "CONTINUE")
        nope = {**NOT_REPRODUCED, "exit_status": 1}
        self.assertEqual(self.decide([finding(nope, "good-faith")]), "ACCEPT")

    def test_hostile_tag_outside_good_faith_model(self):
        self.assertEqual(self.decide([finding(REPRODUCED, "hostile-author")], "good-faith"), "ACCEPT")
        self.assertEqual(self.decide([finding(REPRODUCED, "hostile-author")], "hostile-author"),
                         "CONTINUE")
        self.assertEqual(self.decide([finding(REPRODUCED, "good-faith")], "hostile-author"),
                         "CONTINUE")

    def test_missing_or_unknown_threat_tag_is_unknown(self):
        self.assertEqual(self.decide([finding(REPRODUCED)]), "UNKNOWN")
        self.assertEqual(self.decide([finding(REPRODUCED, "nation-state")]), "UNKNOWN")

    def test_absent_threat_model_is_unknown(self):
        shadow = passes.stop_rule_shadow([finding(NOT_REPRODUCED, "good-faith")], None, [], None, "h")
        self.assertEqual(shadow["decision"], "UNKNOWN")
        self.assertIn("no-threat-model", [r["why"] for r in shadow["reasons"]])
        self.assertIsNone(shadow["threat_model_source"])

    def test_prior_blocking_needs_passing_reverify_on_this_head(self):
        clean = [finding(NOT_REPRODUCED, "good-faith")]
        self.assertEqual(self.decide(clean, prior=["p:1"]), "UNKNOWN")
        self.assertEqual(self.decide(clean, prior=["p:1"], reverify=[
            {"anchor": "p:1", "command": "t", "exit_status": 0, "head": "old"}]), "UNKNOWN")
        self.assertEqual(self.decide(clean, prior=["p:1"], reverify=[
            {"anchor": "p:1", "command": "t", "exit_status": 1, "head": "h"}]), "UNKNOWN")
        self.assertEqual(self.decide(clean, prior=["p:1"], reverify=[
            {"anchor": "p:1", "command": "t", "exit_status": 0, "head": "h"}]), "ACCEPT")

    def test_dismissed_findings_are_not_classified(self):
        """Through the caller's own filter: a dismissed reproduced finding must
        not turn ACCEPT into CONTINUE."""
        dismissed = {**finding(REPRODUCED, "good-faith"), "dismissed": "no scenario"}
        args = mock.Mock(threat_model="good-faith", reverify=None)
        shadow = preclose.stop_rule_shadow(args, [dismissed, finding(NOT_REPRODUCED, "good-faith", "b:2")],
                                           None, "h")
        self.assertEqual(shadow["decision"], "ACCEPT")
        self.assertNotIn("tools/x.py:1", [r["anchor"] for r in shadow["reasons"]])

    def test_reverify_entry_needs_its_command(self):
        clean = [finding(NOT_REPRODUCED, "good-faith")]
        for entry in ({"anchor": "p:1", "exit_status": 0, "head": "h"},
                      {"anchor": "p:1", "command": "  ", "exit_status": 0, "head": "h"}):
            with self.subTest(entry=entry):
                self.assertEqual(self.decide(clean, prior=["p:1"], reverify=[entry]), "UNKNOWN")

    def test_an_unknown_reproduced_finding_is_carried_forward(self):
        """Pass 1 without a threat model: its reproduced finding is unknown, and
        pass 2 with a model still owes its re-verification."""
        pass1 = passes.stop_rule_shadow([finding(REPRODUCED, "good-faith", "p:1")], None, [], None, "h1")
        receipt = {"pass_history": [{"sha": "h1", "epoch": 0, "stop_rule_shadow": pass1}]}
        owed = passes.prior_blocking(receipt)
        self.assertEqual(owed, ["p:1"])
        self.assertEqual(self.decide([finding(NOT_REPRODUCED, "good-faith", "q:2")], prior=owed,
                                     head="h2"), "UNKNOWN")

    def test_an_unknown_pass_never_drops_an_earlier_obligation(self):
        pass1 = passes.stop_rule_shadow([finding(REPRODUCED, "good-faith", "p:1")], "good-faith",
                                        [], None, "h1")
        pass2 = passes.shadow_error("shadow-error:RuntimeError", "good-faith")
        receipt = {"pass_history": [{"sha": "h1", "epoch": 0, "stop_rule_shadow": pass1},
                                    {"sha": "h2", "epoch": 0, "stop_rule_shadow": pass2}]}
        self.assertEqual(passes.prior_blocking(receipt), ["p:1"])

    def test_a_passing_reverify_clears_the_obligation(self):
        pass1 = passes.stop_rule_shadow([finding(REPRODUCED, "good-faith", "p:1")], "good-faith",
                                        [], None, "h1")
        pass2 = passes.stop_rule_shadow([], "good-faith", ["p:1"], [
            {"anchor": "p:1", "command": "t", "exit_status": 0, "head": "h2"}], "h2")
        self.assertEqual(pass2["decision"], "ACCEPT")
        receipt = {"pass_history": [{"sha": "h1", "epoch": 0, "stop_rule_shadow": pass1},
                                    {"sha": "h2", "epoch": 0, "stop_rule_shadow": pass2}]}
        self.assertEqual(passes.prior_blocking(receipt), [])


class RecordedTests(ScratchRepo):  # AC2, AC3, AC4, AC6
    def complete_with(self, findings, *, force=False, threat_model="good-faith", reverify=None):
        path = Path(tempfile.mkdtemp()) / "findings.json"
        path.write_text(json.dumps(findings))
        envelope = path.with_name("envelope.txt")
        envelope.write_text(json.dumps({  # as test_preclose_check's own complete() helper
            "status": "ok", "label": CROSS, "report": {"assumptions": [{"verdict": "confirmed"}]},
            "family": "codex", "posture": "verify", "exit_code": 0, "caller_family": "claude",
            "target_family": "codex",
            "native": [{"type": "thread.started"},
                       {"type": "item.completed", "item": {"type": "agent_message"}}]}))
        if force:
            self.grant_force()
        args = _Args(repo="vitalharmony/hrse", issue=1208, base="base", head="HEAD",
                     findings=str(path), envelope=str(envelope), not_triggered=False,
                     cross_family=False, force=force, threat_model=threat_model,
                     reverify=reverify)
        out = io.StringIO()
        code = None
        with contextlib.redirect_stdout(out):
            try:
                code = preclose.complete(args)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue()

    def receipt(self):
        return preclose.find_receipt("vitalharmony/hrse", 1208) or {}

    def last_shadow(self):
        return passes.current(passes.history(self.receipt()))[-1].get("stop_rule_shadow")

    def test_completion_records_shadow(self):
        self.complete_with([])
        shadow = self.last_shadow()
        self.assertEqual(shadow["decision"], "ACCEPT")
        self.assertEqual(shadow["threat_model"], "good-faith")
        self.assertEqual(shadow["rule_version"], passes.SHADOW_RULE_VERSION)

    def test_flag_threat_model_records_source(self):
        self.complete_with([])
        self.assertEqual(self.last_shadow()["threat_model_source"], "flag")

    def test_forced_completion_records_shadow(self):
        with mock.patch.object(preclose, "emit_force"):
            code, _out = self.complete_with([], force=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.last_shadow()["decision"], "ACCEPT")

    def _observed(self, code, out):
        """Exit code, stdout (temp paths masked) and pass_count."""
        masked = re.sub(r"\b[0-9a-f]{12}\b", "<sha>", re.sub(r"/tmp/\S*", "<tmp>", out))
        return code, masked, self.receipt().get("pass_count")

    def _reference(self, **kwargs):
        """The same, with the shadow disabled."""
        with mock.patch.object(preclose, "stop_rule_shadow", return_value=None):
            code, out = self.complete_with([], **kwargs)
        return self._observed(code, out)

    def test_shadow_error_never_changes_exit_or_count(self):
        reference = self._reference()
        self.setUp()
        with mock.patch.object(passes, "stop_rule_shadow", side_effect=RuntimeError("boom")):
            code, out = self.complete_with([])
        self.assertEqual(self._observed(code, out), reference)
        self.assertEqual(self.last_shadow()["decision"], "UNKNOWN")
        self.assertIn("shadow-error:RuntimeError", self.last_shadow()["reasons"][0]["why"])

    def test_malformed_reverify_is_unknown_never_exits(self):
        with mock.patch.object(preclose, "emit_force"):
            reference = self._reference(force=True)
        for content in ("{not json", '{"a": 1}', None):
            with self.subTest(content=content):
                self.setUp()
                bad = Path(tempfile.mkdtemp()) / "reverify.json"
                if content is not None:  # None: a path that does not exist
                    bad.write_text(content)
                with mock.patch.object(preclose, "emit_force"):
                    code, out = self.complete_with([], force=True, reverify=str(bad))
                self.assertEqual(self._observed(code, out), reference)
                self.assertEqual(self.last_shadow()["reasons"][0]["why"], "reverify-unparseable")

    def test_absent_threat_model_is_recorded_unknown(self):
        self.complete_with([], threat_model=None)
        shadow = self.last_shadow()
        self.assertEqual(shadow["decision"], "UNKNOWN")
        self.assertIn("no-threat-model", [r["why"] for r in shadow["reasons"]])

    def test_one_shadow_event_per_decision(self):
        with mock.patch.object(preclose, "emit_shadow") as emitted:
            self.complete_with([])
        self.assertEqual(emitted.call_count, 1)
        _repo, _issue, _sha, shadow, labels, _ts = emitted.call_args.args
        self.assertEqual(shadow["decision"], "ACCEPT")
        self.assertEqual(labels["site"], "pass")

    def test_shadow_event_carries_ids_and_counts_only(self):
        sent = []
        fake_emit = mock.MagicMock()
        fake_emit.emit.side_effect = lambda events: sent.extend(events) or {"written": 1}
        with mock.patch.dict(sys.modules, {"emit": fake_emit}):
            preclose.emit_shadow("vitalharmony/hrse", 1208, "a" * 40,
                                 passes.stop_rule_shadow([finding(REPRODUCED, "good-faith")],
                                                         "good-faith", [], None, "h"),
                                 {"site": "pass", "pass": 1, "forced": False}, "2026-10-27T00:00:00Z")
        (event,) = sent
        self.assertEqual(event["event_type"], "preclose.stoprule.shadow")
        self.assertEqual(event["attrs"]["decision"], "CONTINUE")
        self.assertEqual(event["attrs"]["n_blocking"], 1)
        self.assertNotIn("reasons", event["attrs"])

    def test_store_failure_never_fails_the_pass(self):
        broken = mock.MagicMock()
        broken.emit.side_effect = OSError("store")
        err = io.StringIO()
        with mock.patch.dict(sys.modules, {"emit": broken}), contextlib.redirect_stderr(err):
            preclose.emit_shadow("vitalharmony/hrse", 1208, "a" * 40,
                                 passes.shadow_error("x"), {"site": "pass"}, "2026-10-27T00:00:00Z")
        self.assertIn("shadow telemetry not written", err.getvalue())

    def test_post_verdict_records_and_emits_the_shadow(self):
        """The decision point itself: post_verdict() writes the shadow on the
        check and emits one event, with prior obligations carried in."""
        findings = Path(tempfile.mkdtemp()) / "pv.json"
        findings.write_text("[]")
        prior = {"pass_history": [{"sha": "b" * 40, "epoch": 0, "stop_rule_shadow":
                                   passes.stop_rule_shadow([finding(REPRODUCED, "good-faith", "p:1")],
                                                           "good-faith", [], None, "b" * 40)}]}
        args = mock.Mock(repo="vitalharmony/harmonic-forge", issue=1189, base="b", head="HEAD",
                         main="origin/main", envelope="e.jsonl", own_model="claude-opus-5-5",
                         findings=str(findings), cross_family_ms="300", threat_model="good-faith",
                         reverify=None)
        written = {}
        with mock.patch.object(preclose, "require_writable"), \
                mock.patch.object(preclose, "receipt_lock", return_value=contextlib.nullcontext()), \
                mock.patch.object(preclose, "registered_repo", return_value=args.repo), \
                mock.patch.object(preclose, "_require_repo_and_head", return_value="a" * 40), \
                mock.patch.object(preclose, "run", return_value=mock.MagicMock(stdout="b" * 40)), \
                mock.patch.object(preclose, "find_receipt", return_value=prior), \
                mock.patch.object(passes, "cap_message", return_value=passes.STICKY_WICKET), \
                mock.patch.object(passes, "current", side_effect=lambda h: h * 2 if len(h) == 1 else h), \
                mock.patch.object(passes, "accepts_entry", return_value=True), \
                mock.patch.object(preclose, "local_patch_id", return_value="p"), \
                mock.patch.object(preclose, "require_recorded_envelope"), \
                mock.patch.object(preclose, "compute_provenance",
                                  return_value=preclose.PROVENANCE_TRIGGERED[0] + " (codex)"), \
                mock.patch.object(preclose, "check_provenance"), \
                mock.patch.object(preclose, "write_receipt",
                                  side_effect=lambda *a, **k: written.update(k["extra"]) or Path("r")), \
                mock.patch.object(preclose, "emit_shadow") as emitted, \
                contextlib.redirect_stdout(io.StringIO()):
            preclose.post_verdict(args)
        shadow = written["post_verdict_check"]["stop_rule_shadow"]
        self.assertEqual(shadow["decision"], "UNKNOWN")  # p:1 owed, no --reverify
        self.assertIn("no-passing-reverify", [r["why"] for r in shadow["reasons"]])
        self.assertEqual(emitted.call_count, 1)
        self.assertEqual(emitted.call_args.args[4]["site"], "post-verdict")

    def test_post_verdict_fields_carry_shadow(self):
        fields = passes.post_verdict_fields({}, "b" * 40, "c" * 40, "p", CROSS, 0, 300,
                                            passes.shadow_error("x"))
        self.assertEqual(fields["post_verdict_check"]["stop_rule_shadow"]["decision"], "UNKNOWN")


class ReportTests(unittest.TestCase):  # AC5
    def row(self, decisions, times):
        entries = [{"completed_at": t, **({"stop_rule_shadow": {"decision": d}} if d else {})}
                   for d, t in zip(decisions, times)]
        return {"issue": "o/r#1", "passes": len(entries), "forced": len(entries) > 2,
                "entries": entries, "post_verdict_check": False, "post_verdict_shadow": None}

    T = ["2026-10-27T00:00:00Z", "2026-10-27T01:00:00Z", "2026-10-27T02:00:00Z",
         "2026-10-27T05:00:00Z"]

    def test_report_counts_unknown_separately(self):
        stats = report.shadow_stats([self.row(["UNKNOWN", "UNKNOWN", "ACCEPT"], self.T[:3])])
        self.assertEqual(stats["counts"]["UNKNOWN"], 2)
        self.assertEqual(stats["counts"]["ACCEPT"], 1)
        text = report.render_shadow([self.row(["UNKNOWN", "UNKNOWN"], self.T[:2])])
        self.assertIn("0 ACCEPT", text)
        self.assertIn("2 UNKNOWN", text)
        self.assertIn("first shadow ACCEPT: n/a", text)

    def test_baseline_is_pass2_to_last_pass(self):
        stats = report.shadow_stats([self.row(["CONTINUE", "CONTINUE", "ACCEPT", "ACCEPT"], self.T)])
        self.assertEqual(stats["to_last"], [4.0])     # pass 2 (01:00) -> last pass (05:00)
        self.assertEqual(stats["to_accept"], [1.0])   # pass 2 -> first ACCEPT at pass 3 (02:00)
        two = report.shadow_stats([self.row(["CONTINUE", "ACCEPT"], self.T[:2])])
        self.assertEqual(two["to_last"], [])          # fewer than three passes: not in the baseline


if __name__ == "__main__":
    unittest.main()
