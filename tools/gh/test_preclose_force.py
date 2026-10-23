#!/usr/bin/env python3
"""harmonic-forge#1169 AC3 end to end: preclose_check's plain --force against
a real scratch repo and the real grant store under the scratch HOME."""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import test_preclose_check as base  # noqa: E402  (ScratchRepo: real git, scratch HOME)

preclose = base.preclose


class ForceGrantGate(base.ScratchRepo):
    def setUp(self) -> None:
        super().setUp()
        self.commit("scripts/ordinary.py")
        self.plan(tier="fast")
        self.complete()

    def grant(self) -> dict:
        return json.loads((Path(os.environ["HOME"]) / ".claude/state/force/H1208.json").read_text())

    def force_plan(self) -> str:
        """`--plan --force` with whatever grant is on disk (no helper grant)."""
        args = {"repo": "vitalharmony/hrse", "issue": 1208, "base": "base", "head": "HEAD",
                "tier": "fast", "force": True, "allow_dirty": False, "allow_repo_mismatch": False}
        return preclose.plan(base._Args(**args))

    def test_a_forced_completion_emits_one_force_event_through_the_entry_point(self) -> None:
        """Preclose pass 1, finding 3: the AC4 event is wired into complete()."""
        store = Path(os.environ["HOME"]) / "telemetry"
        with mock.patch.dict(os.environ, {"HARMONIC_FORGE_TELEMETRY_STORE": str(store)}):
            self.complete(force=True)
        events = [json.loads(line) for path in store.rglob("*.jsonl")
                  for line in path.read_text().splitlines() if line.strip()]
        forced = [e for e in events if e.get("event_type") == "preclose.force"]
        self.assertEqual(len(forced), 1)
        self.assertEqual((forced[0]["attrs"]["granted"], forced[0]["attrs"]["mode"]), (True, "force"))
        self.assertEqual(forced[0]["attrs"]["reason_comment_id"], "1")
        self.assertNotIn('"reason": ', json.dumps(forced[0]))

    def test_a_force_with_no_grant_is_refused(self) -> None:
        with self.assertRaises(SystemExit) as refused:
            self.force_plan()
        self.assertIn("no FORCE grant", str(refused.exception))

    def test_plan_checks_and_complete_consumes_once(self) -> None:
        self.grant_force()
        self.force_plan()
        self.assertEqual(self.grant()["status"], "usable")
        self.complete(force=True)
        self.assertEqual(self.grant()["status"], "consumed")
        with self.assertRaises(SystemExit) as refused:
            self.force_plan()
        self.assertIn("consumed", str(refused.exception))

    def test_a_moved_head_is_refused(self) -> None:
        self.grant_force()
        self.commit("scripts/later.py")
        with self.assertRaises(SystemExit) as refused:
            self.force_plan()
        self.assertIn("head moved", str(refused.exception))

    def test_the_imc_refusal_runs_before_any_grant_is_consulted(self) -> None:
        """Kill check 5: with no grant, an IMC issue is refused for IMC, not
        for the missing grant, so the grant check cannot sit before it."""
        state = Path(os.environ["HOME"]) / ".claude/state/imc"
        state.mkdir(parents=True, exist_ok=True)
        (state / "H1208.waiver.json").write_text(json.dumps({"key": "H1208", "status": "recorded"}))
        with self.assertRaises(SystemExit) as refused:
            self.force_plan()
        self.assertIn("IMC waiver receipt", str(refused.exception))

    def test_an_imc_receipt_still_refuses_a_granted_force(self) -> None:
        self.grant_force()
        state = Path(os.environ["HOME"]) / ".claude/state/imc"
        state.mkdir(parents=True, exist_ok=True)
        (state / "H1208.waiver.json").write_text(json.dumps({"key": "H1208", "status": "recorded"}))
        with self.assertRaises(SystemExit) as refused:
            self.force_plan()
        self.assertIn("IMC waiver receipt", str(refused.exception))
        self.assertEqual(self.grant()["status"], "usable")


class ReserveBeforeWrite(base.ScratchRepo):
    """F1169 sticky-wicket after pass 2: the grant is spent before any forced
    pass is recorded, so a refused spend records nothing."""

    def setUp(self) -> None:
        super().setUp()
        self.commit("scripts/ordinary.py")
        self.plan(tier="fast")
        self.complete()

    def store(self) -> Path:
        return Path(os.environ["HOME"]) / ".claude/state/force/H1208.json"

    def passes(self) -> int:
        receipt = json.loads(next((Path(os.environ["HOME"]) / ".claude/state/preclose").glob("*1208.json")).read_text())
        return len(receipt.get("pass_history", []))

    def test_an_expired_grant_records_no_forced_pass(self) -> None:
        self.grant_force()
        grant = json.loads(self.store().read_text())
        self.store().write_text(json.dumps({**grant, "expires_at": "2001-01-01T00:00:00Z"}))
        before = self.passes()
        with mock.patch.object(self, "grant_force"), self.assertRaises(SystemExit) as refused:
            self.complete(force=True)
        self.assertIn("expired", str(refused.exception))
        self.assertEqual(self.passes(), before)

    def test_a_second_complete_on_one_grant_is_refused_before_any_write(self) -> None:
        self.grant_force()
        self.complete(force=True)
        after_first = self.passes()
        with mock.patch.object(self, "grant_force"), self.assertRaises(SystemExit) as refused:
            self.complete(force=True)
        self.assertIn("consumed", str(refused.exception))
        self.assertEqual(self.passes(), after_first)

    def test_a_write_failure_after_the_reserve_spends_the_grant_and_says_so(self) -> None:
        self.grant_force()
        events: list = []
        with mock.patch.object(preclose, "write_receipt", side_effect=OSError("disk")), \
                mock.patch.object(preclose, "emit_force",
                                  side_effect=lambda *a, **k: events.append(k.get("outcome"))), \
                self.assertRaises(OSError):
            self.complete(force=True)
        self.assertEqual(json.loads(self.store().read_text())["status"], "consumed")
        self.assertEqual(events, ["reserved-not-recorded"])


if __name__ == "__main__":
    unittest.main()
