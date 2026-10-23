#!/usr/bin/env python3
"""harmonic-forge#1169 AC3 end to end: preclose_check's plain --force against
a real scratch repo and the real grant store under the scratch HOME."""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
