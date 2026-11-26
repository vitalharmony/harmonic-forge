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
from test_ci_outage_pass import PENDING, PassCase, RED  # noqa: E402
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

    def test_rf1_a_no_pr_ack_never_takes_the_outage_release(self) -> None:
        """Reforge round 1 (panel): with --ack-no-pr-required applied, CI is absent because no
        PR exists, not because of an outage; the grant is never consulted, nor spent."""
        with ExitStack() as stack:
            self.ci(stack, s0())
            stack.enter_context(mock.patch.object(L.gate_ci, "no_pr_ack_release", lambda *a, **k: False))
            with self.assertRaises(SystemExit) as ctx:
                L._ae_ci_gate("vitalharmony/hrse", 12, self.S, "no PR: a backfill")
        self.assertNotIn("ci-outage", str(ctx.exception))
        self.assertEqual(force_grant.read_grant(KEY)["status"], "usable")

    def test_rf1_a_cross_repo_ae_refuses(self) -> None:
        """Reforge round 1 (cross-family): the AE's CI is another repo's, while the PASS reads
        the issue repo's PR, so the grant would be spent on a round that cannot complete."""
        with ExitStack() as stack:
            self.ci(stack, s0())
            stack.enter_context(mock.patch.object(L, "_cwd_repo_from_git", lambda _: "vitalharmony/other"))
            with self.assertRaises(SystemExit) as ctx:
                L._ae_ci_gate("vitalharmony/hrse", 12, self.S, None)
        self.assertIn("cross-repo AE", str(ctx.exception))
        self.assertEqual(force_grant.read_grant(KEY)["status"], "usable")

    def test_rf1_a_re_run_attempt_refuses(self) -> None:
        """Reforge round 1 (panel): filter=latest returns only a re-run's newest attempt, so
        a red first attempt is in no record; a re-run is never an outage (E3)."""
        rerun = {**job("verify", None, "queued"), "run_attempt": 2}
        self.refused_with("re-run (verify)", s0([rerun]))

    def test_rf1_an_unprotected_branch_refuses(self) -> None:
        """Reforge round 1 (cross-family): no required check means no status read at all, so
        a failed `verify` status is never seen (E2)."""
        self.required = frozenset()
        self.refused_with("requires no check", s0(statuses=[status("verify", "failure")]))

    def test_rf1_a_failed_unrequired_status_refuses(self) -> None:
        """Reforge round 1 (cross-family): the status read kept required contexts only, so a
        failed `lint` status beside an absent `verify` released (E3 scans every status)."""
        self.refused_with("visibly red (lint:failure)", s0(statuses=[status("lint", "failure")]))


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

    def test_rf1_an_incomplete_run_list_refuses(self) -> None:
        """Reforge round 1 (cross-family): the runs list reports 2 runs and lists 1, so a red
        run may be missing; the read is `unknown`, never released (E1, E4)."""
        self.refused("may be incomplete", run=self.gh(checks=PENDING, total_count=2))

    def test_rf1_an_open_pr_with_no_head_refuses(self) -> None:
        """Reforge round 1 (cross-family): a PR entry with a null head never confirms S (E5)."""
        self.refused(E5, run=self.gh(pulls=[{"number": 7, "head": None, "ref": "feat/12-x",
                                             "base": "main", "state": "open"}]))

    def test_rf1_a_refused_pass_spends_nothing(self) -> None:
        """Reforge round 1 (panel): a refused PASS leaves no `.pass` marker, and the next
        PASS on the same record is released."""
        self.refused("CI is RED", run=self.gh(checks=[check("verify", "failure")]))
        self.assertEqual(list((self.state / "ci-outage").glob("*.pass")), [])
        self.assertTrue(self.gate()[0])

    def test_rf1_one_ci_read_decides_the_pass(self) -> None:
        """Reforge round 1 (cross-family): the first CI read is red and a second would read
        pending; the PASS takes one read and refuses on it, never re-reading after a refusal."""
        red, pending, reads = self.gh(checks=[check("verify", "failure")]), self.gh(checks=PENDING), []

        def run(cmd):
            if any("/actions/runs?" in a for a in cmd):
                reads.append(cmd)
            return (red if len(reads) <= 1 else pending)(cmd)
        self.refused("CI is RED", run=run)
        self.assertEqual(len(reads), 1)


if __name__ == "__main__":
    unittest.main()
