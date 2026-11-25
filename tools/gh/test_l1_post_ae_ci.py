#!/usr/bin/env python3
"""harmonic-forge#894 slice 6 steps 4-5: a pre-merge AE posts only on green CI.

Pending waits (exit 3); every other non-green state, an unresolvable repo, an
unreadable required set and a reader exception refuse. The no-PR
acknowledgment releases `absent` or all-skipped only; a `ci-outage` grant
releases a non-red state only beside a `check-full` receipt. `grant_on_main`
and `merged_readonly` AEs never reach the gate (C2)."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import l1_post as L  # noqa: E402

SHA = "c" * 40
REPO = "vitalharmony/harmonic-forge"


class AeCiGate(unittest.TestCase):
    def gate(self, state=None, *, ack=None, required=frozenset({"verify"}), skipped=False,
             release=None, source="o/r", raises=None):
        conclusion = (mock.patch.object(L.gate_ci, "authorizing_conclusion", side_effect=raises)
                      if raises else
                      mock.patch.object(L.gate_ci, "authorizing_conclusion",
                                        return_value=(state, "detail")))
        with mock.patch.object(L, "_cwd_repo_from_git", return_value=source), \
             mock.patch.object(L, "_ci_reader", return_value="READER"), \
             mock.patch.object(L.gate_ci, "required_checks",
                               return_value=None if required is None else set(required)), \
             conclusion as read, \
             mock.patch.object(L.gate_ci, "_required_checks_all_skipped",
                               return_value=skipped) as all_skipped, \
             mock.patch.object(L.gate_ci, "ci_outage_release", return_value=release) as rel:
            out = L._ae_ci_gate(REPO, 894, SHA, ack)
        return out, read, all_skipped, rel

    def refused(self, *args, **kwargs) -> str:
        with self.assertRaises(SystemExit) as ctx:
            self.gate(*args, **kwargs)
        self.assertNotEqual(ctx.exception.code, L._carry_forward.WAIT_EXIT)
        return str(ctx.exception)

    def test_green_posts_and_reads_through_s0(self):
        out, read, _, _ = self.gate("green")
        self.assertEqual(out, "ci-green")
        self.assertEqual(read.call_args.kwargs["reader"], "READER")

    def test_pending_waits_with_exit_3(self):
        with self.assertRaises(SystemExit) as ctx, \
             mock.patch("sys.stderr") as err:
            self.gate("pending")
        self.assertEqual(ctx.exception.code, L._carry_forward.WAIT_EXIT)
        self.assertIn("WAIT: CI is pending", "".join(c.args[0] for c in err.write.call_args_list))

    def test_every_other_state_refuses(self):
        for state in ("absent", "unknown", "red"):
            with self.subTest(state=state):
                self.assertIn(f"CI is {state}", self.refused(state))

    def test_an_unresolvable_repo_refuses(self):
        self.assertIn("cannot resolve the source repo", self.refused("green", source=None))

    def test_an_unreadable_required_set_refuses(self):
        self.assertIn("required checks", self.refused("green", required=None))

    def test_a_reader_exception_refuses(self):
        self.assertIn("raised RuntimeError", self.refused(raises=RuntimeError("gh down")))

    def test_the_no_pr_ack_releases_absent_and_all_skipped_only(self):
        self.assertEqual(self.gate("absent", ack="data migration")[0], "ci-no-pr-acknowledged")
        out, _, all_skipped, _ = self.gate("unknown", ack="x", skipped=True)
        self.assertEqual(out, "ci-no-pr-acknowledged")
        self.assertEqual(all_skipped.call_args.kwargs["reader"], "READER")
        self.assertIn("CI is unknown", self.refused("unknown", ack="x", skipped=False))
        self.assertIn("CI is red", self.refused("red", ack="x"))

    def test_the_ack_never_releases_pending(self):
        with self.assertRaises(SystemExit) as ctx, mock.patch("sys.stderr"):
            self.gate("pending", ack="x")
        self.assertEqual(ctx.exception.code, L._carry_forward.WAIT_EXIT)

    def test_a_ci_outage_grant_releases_a_non_red_state(self):
        for state in ("pending", "absent", "unknown"):
            with self.subTest(state=state), mock.patch("builtins.print"):
                self.assertEqual(self.gate(state, release="grant+receipt")[0], "ci-outage-grant")

    def test_a_ci_outage_grant_never_releases_red(self):
        msg = self.refused("red", release="grant+receipt")
        self.assertIn("CI is red", msg)


class GrantAndReadonlyAeNeverReachTheGate(unittest.TestCase):
    """Test case 10 (C2)."""

    def steps(self, **kw):
        return L._publish_checks("o/r", 1, "ae", SHA, "feat/1-x", ack_overlap=None,
                                 ack_no_pr_required=None, **kw)

    def test_a_pre_merge_ae_dry_run_runs_the_gate(self):
        with mock.patch.object(L, "_ae_ci_gate", return_value="ci-green") as gate:
            steps = self.steps(grant_on_main=False)
            steps[-1]()
        gate.assert_called_once_with("o/r", 1, SHA, None)

    def test_grant_on_main_and_merged_readonly_skip_it(self):
        for kw in ({"grant_on_main": True}, {"grant_on_main": False, "merged_readonly": True}):
            with self.subTest(**kw), \
                 mock.patch.object(L, "_ae_ci_gate", side_effect=AssertionError("gated")):
                self.assertEqual(len(self.steps(**kw)), 1)  # the world checks only


class CiOutageRelease(unittest.TestCase):
    def release(self, *, grant=True, receipt=None, issue=894, sha=SHA):
        fg = sys.modules.get("force_grant")
        if fg is None:
            import force_grant as fg  # noqa: PLC0415
        cr = L.check_receipt
        require = (mock.patch.object(fg, "require", return_value={"comment_url": "u"}) if grant
                   else mock.patch.object(fg, "require", side_effect=fg.GrantRefused("no")))
        with require as req, mock.patch.object(cr, "lookup", return_value=receipt) as look:
            out = L.gate_ci.ci_outage_release(REPO, issue, "Owner/Repo", sha)
        return out, req, look

    def test_a_grant_beside_a_check_full_receipt_releases(self):
        out, req, look = self.release(receipt={"sha": SHA, "source": "check-full"})
        self.assertIn("ci-outage grant u", out)
        req.assert_called_once_with(REPO, 894, SHA, mode="ci-outage")
        look.assert_called_once_with("owner__repo", SHA)

    def test_no_release_without_both(self):
        for kw in ({"grant": False, "receipt": {"sha": SHA, "source": "check-full"}},
                   {"receipt": None},
                   {"receipt": {"sha": SHA, "source": "check"}},
                   {"receipt": {"sha": SHA, "source": "l1-post-scratch"}},
                   {"receipt": {"sha": "d" * 40, "source": "check-full"}},
                   {"issue": None, "receipt": {"sha": SHA, "source": "check-full"}},
                   {"sha": SHA[:12], "receipt": {"sha": SHA, "source": "check-full"}}):
            with self.subTest(**{k: str(v) for k, v in kw.items()}):
                self.assertIsNone(self.release(**kw)[0])


class GatePassHonorsTheOutageGrant(unittest.TestCase):
    """Pitch amendment 1: check_gate_result honors a ci-outage grant for its SHA only."""

    BODY = f"## Lane 3 Gate Results\n\n**Verdict:** PASS\nHead-SHA: {SHA}\n"

    def check(self, state, release, issue=894):
        g = L.gate_ci
        with mock.patch.object(g, "stale_against_pr", return_value=(False, "")), \
             mock.patch.object(g, "render_pending_remote", return_value=None), \
             mock.patch.object(g, "required_checks", return_value={"verify"}), \
             mock.patch.object(g, "authorizing_conclusion", return_value=(state, "d")), \
             mock.patch.object(g, "ci_outage_release", return_value=release) as rel:
            ok, msg = g.check_gate_result(REPO, self.BODY, issue=issue)
        return ok, msg, rel

    def test_a_pending_pass_is_released_by_the_grant(self):
        ok, msg, rel = self.check("pending", "grant+receipt")
        self.assertTrue(ok, msg)
        rel.assert_called_once_with(REPO, 894, REPO, SHA)

    def test_red_is_never_released(self):
        ok, _msg, rel = self.check("red", "grant+receipt")
        self.assertFalse(ok)
        rel.assert_not_called()

    def test_no_issue_means_no_release(self):
        ok, _msg, rel = self.check("pending", "grant+receipt", issue=None)
        self.assertFalse(ok)
        rel.assert_not_called()


if __name__ == "__main__":
    unittest.main()
