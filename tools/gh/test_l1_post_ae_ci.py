#!/usr/bin/env python3
"""harmonic-forge#894 slice 6 steps 4-5 and the pass-2 PATCH: a pre-merge AE
posts only on green CI.

Pending waits (exit 3); every other non-green state, an unresolvable repo, an
unreadable required set and a reader exception refuse. The no-PR
acknowledgment releases only by `gate_ci.no_pr_ack_release`, the rule
`check_gate_result` uses (A1), and only when `require_open_pr` applied it (A2).
A `ci-outage` grant releases a non-red state beside a check-full receipt at the
current forge HEAD, is consumed by the post, and is named in the AE's footer;
Lane 3's PASS releases only against that consumed grant (A3). `grant_on_main`
and `merged_readonly` AEs never reach the gate (C2)."""
import ast
import itertools
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import l1_post as L  # noqa: E402

SHA = "c" * 40
REPO = "vitalharmony/harmonic-forge"
GRANT = {"granted_at": "2026-11-25T00:00:00Z", "comment_id": "777", "comment_url": "u"}


class AeCiGate(unittest.TestCase):
    def gate(self, state=None, *, ack=None, required=frozenset({"verify"}), no_pr=False,
             grant=None, source="o/r", raises=None):
        conclusion = (mock.patch.object(L.gate_ci, "authorizing_conclusion", side_effect=raises)
                      if raises else
                      mock.patch.object(L.gate_ci, "authorizing_conclusion",
                                        return_value=(state, "detail")))
        with mock.patch.object(L, "_cwd_repo_from_git", return_value=source), \
             mock.patch.object(L, "_ci_reader", return_value="READER"), \
             mock.patch.object(L.gate_ci, "required_checks",
                               return_value=None if required is None else set(required)), \
             conclusion as read, \
             mock.patch.object(L.gate_ci, "no_pr_ack_release", return_value=no_pr) as rule, \
             mock.patch.object(L.gate_ci, "ci_outage_grant", return_value=grant) as og:
            out = L._ae_ci_gate(REPO, 894, SHA, ack)
        L._CI_OUTAGE.pop(SHA, None)
        return out, read, rule, og

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

    def test_the_ack_releases_only_by_the_shared_rule(self):
        out, _, rule, _ = self.gate("absent", ack="data migration", no_pr=True)
        self.assertEqual(out, "ci-no-pr-acknowledged")
        self.assertEqual(rule.call_args.args, ("o/r", SHA, {"verify"}, "absent"))
        self.assertEqual(rule.call_args.kwargs["reader"], "READER")
        self.assertIn("CI is absent", self.refused("absent", ack="x", no_pr=False))

    def test_a_ci_outage_grant_releases_a_non_red_state_and_is_kept_for_the_post(self):
        for state in ("pending", "absent", "unknown"):
            with self.subTest(state=state), mock.patch("builtins.print"), \
                 mock.patch.object(L.gate_ci, "authorizing_conclusion", return_value=(state, "d")), \
                 mock.patch.object(L, "_cwd_repo_from_git", return_value="o/r"), \
                 mock.patch.object(L, "_ci_reader", return_value="R"), \
                 mock.patch.object(L.gate_ci, "required_checks", return_value={"verify"}), \
                 mock.patch.object(L.gate_ci, "no_pr_ack_release", return_value=False), \
                 mock.patch.object(L.gate_ci, "ci_outage_grant", return_value=GRANT):
                self.assertEqual(L._ae_ci_gate(REPO, 894, SHA, None), "ci-outage-grant")
                self.assertIs(L._CI_OUTAGE.pop(SHA), GRANT)

    def test_a_ci_outage_grant_never_releases_red(self):
        self.assertIn("CI is red", self.refused("red", grant=GRANT))


class AppliedAck(unittest.TestCase):
    """PATCH A2: the ack is evidence of an override that was used."""

    def test_an_applied_override_passes_the_ack(self):
        self.assertEqual(L._applied_ack([L.ACK_APPLIED_CHECK], "why", "b"), "why")

    def test_an_ack_with_an_open_pr_refuses(self):
        with self.assertRaises(SystemExit) as ctx:
            L._applied_ack(["pr-open"], "why", "feat/1-x")
        self.assertIn("an open PR exists for feat/1-x", str(ctx.exception))

    def test_no_ack_or_an_unread_pr_state_is_none(self):
        self.assertIsNone(L._applied_ack(["pr-open"], None, "b"))
        self.assertIsNone(L._applied_ack(None, "why", "b"))


class NoPrParity(unittest.TestCase):
    """B4: the AE gate's no-PR decision equals check_gate_result's for the same
    world, over open PR x main tip x CI state (pitch amendment 3)."""

    BODY = f"## Lane 3 Gate Results\n\n**Verdict:** PASS\nHead-SHA: {SHA}\n"

    def world(self, prs, tip, state, skipped):
        g = L.gate_ci
        return [mock.patch.object(g, "open_prs_containing", return_value=prs),
                mock.patch.object(g, "sha_is_current_main_tip", return_value=tip),
                mock.patch.object(g, "_required_checks_all_skipped", return_value=skipped),
                mock.patch.object(g, "authorizing_conclusion", return_value=(state, "d")),
                mock.patch.object(g, "required_checks", return_value={"verify"}),
                mock.patch.object(g, "ci_outage_grant", return_value=None),
                mock.patch.object(g, "stale_against_pr", return_value=(False, "")),
                mock.patch.object(g, "render_pending_remote", return_value=None)]

    def test_the_two_gates_agree(self):
        cases = itertools.product(([], [{"number": 1}], None), (True, False, None),
                                  (("absent", False), ("unknown", True), ("unknown", False),
                                   ("pending", False), ("red", False)))
        for prs, tip, (state, skipped) in cases:
            with self.subTest(prs=prs, tip=tip, state=state, skipped=skipped):
                patches = self.world(prs, tip, state, skipped)
                for p in patches:
                    p.start()
                try:
                    gate_ok = L.gate_ci.check_gate_result(REPO, self.BODY, ack_no_pr_required="x")[0]
                    with mock.patch.object(L, "_cwd_repo_from_git", return_value=REPO), \
                         mock.patch.object(L, "_ci_reader", return_value=None), \
                         mock.patch("sys.stderr"):
                        try:
                            ae_ok = L._ae_ci_gate(REPO, 894, SHA, "x") == "ci-no-pr-acknowledged"
                        except SystemExit:
                            ae_ok = False
                finally:
                    for p in patches:
                        p.stop()
                self.assertEqual(ae_ok, gate_ok)


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


class PostKindRunsTheGate(unittest.TestCase):
    """The real post runs the gate, passes the ack only when applied, and spends
    a ci-outage grant immediately before publishing (A2, A3)."""

    def post(self, *, gate_out="ci-green", pr_names=("pr-open #7",), ack=None, consume=None,
             grant_on_main=False):
        captured = {"order": []}

        def comment(repo, issue, text):
            captured["order"].append("post")
            captured["text"] = text
            return "https://github.com/o/r/issues/1#issuecomment-1", 1

        def gate(repo, issue, sha, applied):
            captured["applied"] = applied
            if gate_out == "ci-outage-grant":
                L._CI_OUTAGE[sha] = GRANT
            return gate_out

        def spend(*a):
            captured["order"].append("consume")
            if consume is not None:
                raise consume
        try:
            with mock.patch.object(L, "comment_body", side_effect=comment), \
                 mock.patch.object(L, "world_checks", return_value=([], [])), \
                 mock.patch.object(L, "require_open_pr", return_value=(list(pr_names), [], [])), \
                 mock.patch.object(L, "validate_sticky_wicket"), \
                 mock.patch.object(L, "write_receipt"), \
                 mock.patch.object(L, "record_overlap_rulings"), \
                 mock.patch.object(L, "active_worktree_branches", return_value=[]), \
                 mock.patch.object(L, "_afk_toggle", None), \
                 mock.patch.object(L, "_tasklist_owed", None), \
                 mock.patch.object(L.gate_ci, "consume_ci_outage_grant", side_effect=spend), \
                 mock.patch.object(L, "_ae_ci_gate", side_effect=gate) as g:
                L._post_kind("o/r", 1, "ae", "## AE\n", SHA, "feat/1-x",
                             ack_no_pr_required=ack, grant_on_main=grant_on_main)
        finally:
            L._CI_OUTAGE.pop(SHA, None)
        return captured, g

    def test_a_pre_merge_ae_runs_the_gate_and_records_it(self):
        captured, gate = self.post()
        gate.assert_called_once()
        self.assertIn("ci-green", captured["text"].rsplit("checks=", 1)[1])

    def test_a_grant_on_main_ae_never_runs_it(self):
        captured, gate = self.post(grant_on_main=True)
        gate.assert_not_called()

    def test_the_ack_reaches_the_gate_only_when_applied(self):
        captured, _ = self.post(pr_names=(L.ACK_APPLIED_CHECK,), ack="migration")
        self.assertEqual(captured["applied"], "migration")
        with self.assertRaises(SystemExit):
            self.post(pr_names=("pr-open",), ack="migration")

    def test_an_outage_ae_consumes_then_posts_and_names_the_grant(self):
        captured, _ = self.post(gate_out="ci-outage-grant")
        self.assertEqual(captured["order"], ["consume", "post"])
        self.assertIn(f"**CI substitute:** check-full receipt {SHA}", captured["text"])
        self.assertIn(" ci-override=777;", captured["text"])

    def test_a_refused_consume_posts_nothing(self):
        with self.assertRaises(SystemExit):
            self.post(gate_out="ci-outage-grant", consume=RuntimeError("consumed already"))


class B2RealPostPath(unittest.TestCase):
    """B2: through `_post_kind`, absent or unknown CI runs the full check, green
    or pending the local path (test cases 2 and 9 end to end)."""

    def path(self, ci_state):
        seen = {}

        def static(sha, branch, dry_run=False, **kw):
            seen["mode"] = kw.get("mode", "full")
            return ["mise-check"], None, {"result": "pass"}
        with mock.patch.object(L, "orphan_report"), \
             mock.patch.object(L, "refresh_main"), \
             mock.patch.object(L, "world_checks", return_value=([], [])), \
             mock.patch.object(L, "_refuse_if_ci_red", return_value=ci_state), \
             mock.patch.object(L, "_audit_sampled", return_value=False), \
             mock.patch.object(L, "static_checks", side_effect=static), \
             mock.patch.object(L, "require_open_pr", return_value=(["pr-open"], [], [])), \
             mock.patch.object(L, "pr_issue_marker", return_value="<!-- m -->"), \
             mock.patch.object(L, "validate_sticky_wicket"), \
             mock.patch.object(L, "comment_body", return_value=("https://x/1#issuecomment-1", 1)), \
             mock.patch.object(L, "write_receipt"), \
             mock.patch.object(L, "record_overlap_rulings"), \
             mock.patch.object(L, "active_worktree_branches", return_value=[]), \
             mock.patch.object(L, "_afk_toggle", None), \
             mock.patch.object(L, "_tasklist_owed", None):
            L._post_kind("o/r", 1, "ready-for-l3", "## r4l3\n", SHA, "feat/1-x")
        return seen["mode"]

    def test_the_real_post_path_follows_ci(self):
        for state, want in (("green", "local"), ("pending", "local"), ("absent", "full"),
                            ("unknown", "full"), (None, "full")):
            with self.subTest(state=state):
                self.assertEqual(self.path(state), want)


class B3EveryCiReadPassesTheReader(unittest.TestCase):
    """B3 (test case 11): every call in l1_post.py to a gate_ci CI-state reader
    passes `reader=`; a new site that forgets it fails here."""

    READERS = {"ci_conclusion", "authorizing_conclusion", "_read_every_attempt",
               "_status_records", "_required_checks_all_skipped", "no_pr_ack_release"}

    def test_inventory(self):
        tree = ast.parse(Path(L.__file__).read_text(encoding="utf-8"))
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr in self.READERS
                 and isinstance(n.func.value, ast.Name) and n.func.value.id == "gate_ci"]
        self.assertGreaterEqual(len(calls), 5)
        missing = [n.lineno for n in calls if "reader" not in {k.arg for k in n.keywords}]
        self.assertEqual(missing, [], f"gate_ci CI reads without reader= at lines {missing}")


class CiOutageGrant(unittest.TestCase):
    def outage(self, *, grant=True, receipt=None, forge="f" * 40, issue=894, sha=SHA):
        import force_grant as fg  # noqa: PLC0415
        cr = L.check_receipt
        require = (mock.patch.object(fg, "require", return_value=GRANT) if grant
                   else mock.patch.object(fg, "require", side_effect=fg.GrantRefused("no")))
        with require as req, mock.patch.object(cr, "lookup", return_value=receipt) as look, \
             mock.patch.object(cr, "forge_sha", return_value=forge):
            out = L.gate_ci.ci_outage_grant(REPO, issue, "Owner/Repo", sha)
        return out, req, look

    def ok_receipt(self):
        return {"sha": SHA, "source": "check-full", "forge_sha": "f" * 40}

    def test_a_grant_beside_a_current_check_full_receipt_releases(self):
        out, req, look = self.outage(receipt=self.ok_receipt())
        self.assertIs(out, GRANT)
        req.assert_called_once_with(REPO, 894, SHA, mode="ci-outage")
        look.assert_called_once_with("owner__repo", SHA)

    def test_no_release_without_every_condition(self):
        stale = {**self.ok_receipt(), "forge_sha": "e" * 40}
        for kw in ({"grant": False, "receipt": self.ok_receipt()},
                   {"receipt": None},
                   {"receipt": {**self.ok_receipt(), "source": "check"}},
                   {"receipt": {**self.ok_receipt(), "sha": "d" * 40}},
                   {"receipt": stale},
                   {"receipt": self.ok_receipt(), "forge": None},
                   {"issue": None, "receipt": self.ok_receipt()},
                   {"sha": SHA[:12], "receipt": self.ok_receipt()}):
            with self.subTest(**{k: str(v)[:30] for k, v in kw.items()}):
                self.assertIsNone(self.outage(**kw)[0])


class PassReleasesOnlyAConsumedNamedGrant(unittest.TestCase):
    """A3, the PASS side: a consumed ci-outage grant for this SHA, named by the
    AE's ci-override footer, beside a current receipt."""

    def release(self, grant, override="777", receipt=True):
        import force_grant as fg  # noqa: PLC0415
        with mock.patch.object(fg, "read_grant", return_value=grant), \
             mock.patch.object(L.gate_ci, "_check_full_receipt_holds", return_value=receipt):
            return L.gate_ci.ci_outage_pass_release(REPO, 894, REPO, SHA, override)

    def consumed(self, **over):
        return {"mode": "ci-outage", "status": "consumed", "consumed_sha": SHA,
                "comment_id": "777", "comment_url": "u", **over}

    def test_a_consumed_named_grant_releases(self):
        self.assertIn("consumed ci-outage grant", self.release(self.consumed()))

    def test_anything_less_does_not(self):
        for grant, override, receipt in ((self.consumed(status="usable"), "777", True),
                                         (self.consumed(consumed_sha="d" * 40), "777", True),
                                         (self.consumed(mode="route"), "777", True),
                                         (self.consumed(), "778", True),
                                         (self.consumed(), None, True),
                                         (self.consumed(), "777", False),
                                         (None, "777", True)):
            with self.subTest(grant=str(grant)[:40], override=override, receipt=receipt):
                self.assertIsNone(self.release(grant, override, receipt))

    def test_check_gate_result_releases_pending_only_with_the_override(self):
        g = L.gate_ci
        body = f"## Lane 3 Gate Results\n\n**Verdict:** PASS\nHead-SHA: {SHA}\n"
        with mock.patch.object(g, "stale_against_pr", return_value=(False, "")), \
             mock.patch.object(g, "render_pending_remote", return_value=None), \
             mock.patch.object(g, "required_checks", return_value={"verify"}), \
             mock.patch.object(g, "authorizing_conclusion", return_value=("pending", "d")), \
             mock.patch.object(g, "ci_outage_pass_release", return_value="released") as rel:
            self.assertTrue(g.check_gate_result(REPO, body, issue=894, ci_override="777")[0])
            rel.assert_called_once_with(REPO, 894, REPO, SHA, "777")
        with mock.patch.object(g, "stale_against_pr", return_value=(False, "")), \
             mock.patch.object(g, "render_pending_remote", return_value=None), \
             mock.patch.object(g, "required_checks", return_value={"verify"}), \
             mock.patch.object(g, "authorizing_conclusion", return_value=("red", "d")), \
             mock.patch.object(g, "ci_outage_pass_release", return_value="released") as rel:
            self.assertFalse(g.check_gate_result(REPO, body, issue=894, ci_override="777")[0])
            rel.assert_not_called()


if __name__ == "__main__":
    unittest.main()
