#!/usr/bin/env python3
"""AC6 (harmonic-forge#895): preclose_check's IMC refusals, keyed on the
waiver receipt's presence. State lives in a temp dir, never the live store."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import imc_waiver  # noqa: E402
import test_preclose_check as base  # noqa: E402

preclose = base.preclose
KEY = "H1208"


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class ImcPrecloseTests(base.ScratchRepo):
    def setUp(self) -> None:
        super().setUp()
        self.state = Path(tempfile.mkdtemp())
        patcher = patch.object(imc_waiver, "IMC_STATE_DIR", self.state)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.commit("scripts/a.py")

    def hook_record(self, granted: datetime) -> None:
        (self.state / f"{KEY}.json").write_text(json.dumps(
            {"key": KEY, "repo": "vitalharmony/hrse", "issue": 1208,
             "granted_at": _iso(granted), "session_id": "s", "line": "BATCH H1208 IMC H1208",
             "hook_version": "1"}))

    def receipt(self, granted: datetime) -> None:
        (self.state / f"{KEY}.waiver.json").write_text(json.dumps(
            {"key": KEY, "granted_at": _iso(granted), "comment_url": "u", "branch": "fix/1208-x"}))

    # --plan ---------------------------------------------------------------
    def test_plan_refuses_live_hook_record_without_receipt(self) -> None:
        self.hook_record(datetime.now(timezone.utc))
        with self.assertRaises(SystemExit) as refused:
            self.plan(tier="fast")
        self.assertIn("no matching waiver receipt", str(refused.exception))

    def test_plan_refuses_receipt_with_a_different_granted_at(self) -> None:
        now = datetime.now(timezone.utc)
        self.hook_record(now)
        self.receipt(now - timedelta(hours=1))
        with self.assertRaises(SystemExit):
            self.plan(tier="fast")

    def test_plan_proceeds_with_matching_receipt(self) -> None:
        now = datetime.now(timezone.utc)
        self.hook_record(now)
        self.receipt(now)
        self.assertIn("refuters:", self.plan(tier="fast"))

    def test_plan_ignores_an_expired_hook_record(self) -> None:
        self.hook_record(datetime.now(timezone.utc) - timedelta(hours=13))
        self.assertIn("refuters:", self.plan(tier="fast"))

    def test_plan_refuses_allow_dirty_under_receipt(self) -> None:
        self.receipt(datetime.now(timezone.utc))
        with self.assertRaises(SystemExit) as refused:
            self.plan(tier="fast", allow_dirty=True)
        self.assertIn("--allow-dirty", str(refused.exception))

    def test_plan_refuses_force_under_receipt(self) -> None:
        self.receipt(datetime.now(timezone.utc))
        with self.assertRaises(SystemExit) as refused:
            self.plan(tier="fast", force=True)
        self.assertIn("--force", str(refused.exception))

    def test_plan_allows_force_and_dirty_without_receipt(self) -> None:
        self.assertIn("refuters:", self.plan(tier="fast", allow_dirty=True))

    # gate -----------------------------------------------------------------
    def test_gate_forced_after_hook_record_and_grant_expired(self) -> None:
        """Keyed on the receipt, not liveness: the hook record is long expired
        and no BATCH grant exists at all, and the gate is still forced even
        though one finding survived (which would otherwise not trigger)."""
        old = datetime.now(timezone.utc) - timedelta(days=2)
        self.hook_record(old)
        self.receipt(old)
        args = base._Args(repo="vitalharmony/hrse", issue=1208, base="base", head="HEAD",
                          findings=self.findings_file([base.ANCHORED]), cross_family=False)
        required, why, surviving, _ = preclose.gate_decision(args)
        self.assertEqual(surviving, 1)
        self.assertTrue(required)
        self.assertIn("IMC", why)

    def test_gate_not_forced_without_receipt(self) -> None:
        args = base._Args(repo="vitalharmony/hrse", issue=1208, base="base", head="HEAD",
                          findings=self.findings_file([base.ANCHORED]), cross_family=False)
        self.assertFalse(preclose.gate_decision(args)[0])

    # --complete -----------------------------------------------------------
    def _planned_with_receipt(self) -> None:
        now = datetime.now(timezone.utc)
        self.hook_record(now)
        self.receipt(now)
        self.plan(tier="fast")

    def test_complete_refuses_not_triggered(self) -> None:
        self._planned_with_receipt()
        with self.assertRaises(SystemExit) as refused:
            self.complete([base.ANCHORED], not_triggered=True)
        self.assertIn("--not-triggered", str(refused.exception))

    def test_complete_refuses_in_family_fallback(self) -> None:
        self._planned_with_receipt()
        with self.assertRaises(SystemExit) as refused:
            self.complete([], envelope_label=base.FALLBACK)
        self.assertIn("in-family fallback", str(refused.exception))

    def test_complete_refuses_force(self) -> None:
        self._planned_with_receipt()
        with self.assertRaises(SystemExit) as refused:
            self.complete([], force=True)
        self.assertIn("--force", str(refused.exception))

    def test_complete_refuses_reforge(self) -> None:
        self._planned_with_receipt()
        with self.assertRaises(SystemExit) as refused:
            with patch.object(base._Args, "reforge", True, create=True):
                self.complete([])
        self.assertIn("--reforge", str(refused.exception))

    def test_complete_records_cross_family_under_receipt(self) -> None:
        self._planned_with_receipt()
        out = self.complete([])
        self.assertIn("cross-family: required", out)

    def test_complete_with_survivor_requires_cross_family_under_receipt(self) -> None:
        """Without IMC, one survivor means not-triggered; under IMC the
        cross-family label is still demanded."""
        self._planned_with_receipt()
        out = self.complete([base.ANCHORED])
        self.assertIn("IMC waiver receipt exists", out)

    # --cluster-verdict ----------------------------------------------------
    def test_cluster_verdict_force_refused_under_receipt(self) -> None:
        self.receipt(datetime.now(timezone.utc))
        args = base._Args(repo="vitalharmony/hrse", issue=1208, force=True,
                          cluster_verdict="PATCH", comment_url="u")
        with self.assertRaises(SystemExit) as refused:
            preclose.cluster_verdict(args)
        self.assertIn("--cluster-verdict with --force", str(refused.exception))


class NotAGateTests(unittest.TestCase):
    def test_not_a_gate_names_imc_closure_authority(self) -> None:
        self.assertIn("operator's IMC line, never this output", preclose.NOT_A_GATE)
        self.assertIn("self-grading with one level of indirection",
                      (Path(__file__).resolve().parents[2] / "skills" / "preclose-check"
                       / "SKILL.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
