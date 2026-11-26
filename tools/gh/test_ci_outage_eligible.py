#!/usr/bin/env python3
"""harmonic-forge#1565 REFORGE (sticky-wicket issuecomment-7182048369): the CI half
of the ci-outage release is `gate_ci.outage_eligible`, judged on the one snapshot
gate_ci read. Each case is a preclose round-2 survivor (issuecomment-7182009666)
or one predicate rule (E1-E5), driven end to end through l1_post's AE or
`require_green_ci`'s PASS, in the real temp stores of test_ci_outage_ae.World.
"""
from __future__ import annotations

import json
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ci_outage_ae import KEY, L, World, force_grant, job, s0, status  # noqa: E402
from test_ci_outage_pass import PassCase, RED  # noqa: E402
from test_gate_ci import check  # noqa: E402

E5 = "not confirmed as the head of an open PR"


def bot_status(context: str, state: str) -> dict:
    """A commit status as fake_gh streams it (`creator` already flattened)."""
    return {"context": context, "state": state, "creator": "github-actions[bot]",
            "updated_at": "2026-09-09T00:00:00Z"}


class AeEligibility(World, unittest.TestCase):
    """AE side: every case holds a usable grant and a current check-full sidecar."""
    required = frozenset({"verify"})

    def setUp(self) -> None:
        self.build()
        self.grant()
        self.check_full()

    def ci(self, stack: ExitStack, reader) -> None:
        World.ci(self, stack, reader)
        stack.enter_context(mock.patch.object(L.gate_ci, "required_checks", lambda *a, **k: set(self.required)))

    def refused_with(self, needle: str, reader) -> None:
        with self.assertRaises(SystemExit) as ctx:
            self.post_ae(reader=reader)
        self.assertNotEqual(ctx.exception.code, L._carry_forward.WAIT_EXIT)
        self.assertIn(needle, str(ctx.exception))
        self.assertEqual((self.comments, force_grant.read_grant(KEY)["status"]), ([], "usable"))

    def test_r2f1_a_red_status_behind_a_full_status_page_refuses(self) -> None:
        """Round 2 survivor 1: 99 decoy statuses plus a red required `verify` status fill the
        100-entry page, which reads `unknown`; the red status is never seen (E2, E4)."""
        decoys = [status(f"decoy-{n}", "success") for n in range(99)]
        self.refused_with("CI is unknown", s0(statuses=[status("verify", "failure")] + decoys))

    def test_e1_a_failed_actions_read_refuses(self) -> None:
        """E1: the runs read fails (502), so nothing was read to rule out red."""
        def reader(path: str):
            return (502, None) if "/actions/runs?" in path else s0()(path)
        self.refused_with("CI is unknown", reader)

    def test_r2f3_a_red_status_beside_an_absent_required_check_refuses(self) -> None:
        """Round 2 survivor 3: `ui-approval` failed while `verify` is not yet created;
        `_judge_runs` returns `absent` before it reaches `red` (E3 scans every record)."""
        self.required = frozenset({"verify", "ui-approval"})
        self.refused_with("visibly red (ui-approval:failure)", s0(statuses=[status("ui-approval", "failure")]))


class PassEligibility(PassCase):
    """PASS side: every case starts from TC 1's genuine bound AE at S plus a sweep."""

    def test_r2f2_a_failed_stale_head_read_refuses(self) -> None:
        """Round 2 survivor 2: the commits/S/pulls read fails, so S is not shown to be its
        PR's head; `stale_against_pr` answers None, never "not stale" (E5)."""
        self.refused(E5, run=self.gh(fail=("/pulls",)))

    def test_e5_no_open_pr_for_the_sha_refuses(self) -> None:
        self.refused(E5, run=self.gh(pulls=[]))

    def test_r2f3_a_red_status_beside_an_absent_required_check_refuses(self) -> None:
        ok, message = self.gate(run=self.gh(required={"verify", "ui-approval"},
                                            statuses=[bot_status("ui-approval", "failure")]))
        self.assertFalse(ok)
        self.assertIn("absent —", message)
        self.assertIn(f"{RED} (ui-approval:failure)", message)

    def test_r2f4_a_same_named_job_in_another_workflow_does_not_hide_a_red_job(self) -> None:
        """Round 2 survivor 4: `build` failed in run 1; a newer `build` in another workflow
        succeeded, so `_latest_per_name` keeps only the green one (E3 scans before dedupe)."""
        newer = {"name": "build", "status": "completed", "conclusion": "success",
                 "completed_at": "2026-09-10T00:00:00Z"}
        ok, message = self.gate(run=self.gh(
            checks=[check("verify", None, "in_progress"), check("build", "failure")],
            run_status="in_progress", extra_runs=[{"status": "completed", "jobs": [newer]}]))
        self.assertFalse(ok)
        self.assertIn("pending —", message)
        self.assertIn(f"{RED} (build:failure)", message)

    def test_a_complete_snapshot_releases_and_records_why(self) -> None:
        """Positive control for every case above: the same world with no red and a known head."""
        ok, line = self.gate(run=self.gh(checks=[check("verify", None, "in_progress"), job("build")]))
        self.assertTrue(ok, line)
        self.assertIn("released (CI is pending with no red record)", line)
        self.assertTrue(json.loads(next((self.state / "ci-outage").glob("*.pass")).read_text())["at"])


if __name__ == "__main__":
    unittest.main()
