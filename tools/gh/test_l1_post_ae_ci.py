#!/usr/bin/env python3
"""harmonic-forge#894 slice 6 steps 4-5 and the pass-2 PATCH: a pre-merge AE
posts only on green CI.

Pending waits (exit 3); every other non-green state, an unresolvable repo, an
unreadable required set and a reader exception refuse. The no-PR
acknowledgment releases only by `gate_ci.no_pr_ack_release`, the rule
`check_gate_result` uses (A1), and only when `require_open_pr` applied it (A2).
With no ci-outage grant (harmonic-forge#1565, test_ci_outage_ae.py) a CI outage
waits or refuses like any other non-green read.
`grant_on_main` and `merged_readonly` AEs never reach the gate (C2)."""
import ast
import itertools
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import l1_post as L  # noqa: E402

SHA = "c" * 40
REPO = "vitalharmony/harmonic-forge"


def setUpModule():
    """harmonic-forge#1565: a non-green read consults the ci-outage grant store;
    an empty temp store, never the live one."""
    global _STORE
    _STORE = tempfile.TemporaryDirectory()
    L.ci_outage.force_grant.FORCE_STATE_DIR = Path(_STORE.name)


def tearDownModule():
    L.ci_outage.force_grant.FORCE_STATE_DIR = None
    _STORE.cleanup()


class AeCiGate(unittest.TestCase):
    def gate(self, state=None, *, ack=None, required=frozenset({"verify"}), no_pr=False,
             source="o/r", raises=None):
        conclusion = (mock.patch.object(L.gate_ci, "authorizing_conclusion", side_effect=raises)
                      if raises else
                      mock.patch.object(L.gate_ci, "authorizing_conclusion",
                                        return_value=(state, "detail")))
        with mock.patch.object(L, "_cwd_repo_from_git", return_value=source), \
             mock.patch.object(L, "_ci_reader", return_value="READER"), \
             mock.patch.object(L.gate_ci, "required_checks",
                               return_value=None if required is None else set(required)), \
             conclusion as read, \
             mock.patch.object(L.gate_ci, "no_pr_ack_release", return_value=no_pr) as rule:
            out = L._ae_ci_gate(REPO, 894, SHA, ack)
        return out, read, rule

    def refused(self, *args, **kwargs) -> str:
        with self.assertRaises(SystemExit) as ctx:
            self.gate(*args, **kwargs)
        self.assertNotEqual(ctx.exception.code, L._carry_forward.WAIT_EXIT)
        return str(ctx.exception)

    def test_green_posts_and_reads_through_s0(self):
        out, read, _ = self.gate("green")
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
                message = self.refused(state)
                self.assertIn(f"CI is {state}", message)
                self.assertIn("Fix or re-run CI", message)
                # The existing operator paths only; no CI-outage override line.
                self.assertNotIn("ci-outage", message)
                # The no-PR ack is named only where it can release (never on red).
                if state == "red":
                    self.assertNotIn("--ack-no-pr-required", message)
                else:
                    self.assertIn("--ack-no-pr-required", message)

    def test_an_unresolvable_repo_refuses(self):
        self.assertIn("cannot resolve the source repo", self.refused("green", source=None))

    def test_an_unreadable_required_set_refuses(self):
        self.assertIn("required checks", self.refused("green", required=None))

    def test_a_reader_exception_refuses(self):
        self.assertIn("raised RuntimeError", self.refused(raises=RuntimeError("gh down")))

    def test_the_ack_releases_only_by_the_shared_rule(self):
        out, _, rule = self.gate("absent", ack="data migration", no_pr=True)
        self.assertEqual(out, "ci-no-pr-acknowledged")
        self.assertEqual(rule.call_args.args, ("o/r", SHA, {"verify"}, "absent"))
        self.assertEqual(rule.call_args.kwargs["reader"], "READER")
        self.assertIn("CI is absent", self.refused("absent", ack="x", no_pr=False))

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
    """The real post runs the gate and passes the ack only when applied (A2)."""

    def post(self, *, gate_out="ci-green", pr_names=("pr-open #7",), ack=None,
             grant_on_main=False):
        captured = {}

        def comment(repo, issue, text):
            captured["text"] = text
            return "https://github.com/o/r/issues/1#issuecomment-1", 1

        def gate(repo, issue, sha, applied):
            captured["applied"] = applied
            return gate_out
        with mock.patch.object(L, "comment_body", side_effect=comment), \
             mock.patch.object(L, "world_checks", return_value=([], [])), \
             mock.patch.object(L, "require_open_pr", return_value=(list(pr_names), [], [])), \
             mock.patch.object(L, "validate_sticky_wicket"), \
             mock.patch.object(L, "write_receipt"), \
             mock.patch.object(L, "record_overlap_rulings"), \
             mock.patch.object(L, "active_worktree_branches", return_value=[]), \
             mock.patch.object(L, "_afk_toggle", None), \
             mock.patch.object(L, "_tasklist_owed", None), \
             mock.patch.object(L, "_ae_ci_gate", side_effect=gate) as g:
            L._post_kind("o/r", 1, "ae", "## AE\n", SHA, "feat/1-x",
                         ack_no_pr_required=ack, grant_on_main=grant_on_main)
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
        # PENDING_WITH_RED: a red job beside a running verify (harmonic-forge#894
        # post-verdict finding 4, third check) takes the full check on the real post.
        for state, want in (("green", "local"), ("pending", "local"), ("absent", "full"),
                            ("unknown", "full"), (None, "full"),
                            (L.PENDING_WITH_RED, "full")):
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


class P6InventoryChecksTheValue(unittest.TestCase):
    """P6: every reader= is _ci_reader(...) or a name bound from it."""

    def test_reader_values_are_the_s0_reader(self):
        tree = ast.parse(Path(L.__file__).read_text(encoding="utf-8"))
        bound = {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
                 and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name)
                 and n.value.func.id == "_ci_reader" for t in n.targets if isinstance(t, ast.Name)}
        bad = []
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and isinstance(n.func.value, ast.Name) and n.func.value.id == "gate_ci"):
                continue
            for k in n.keywords:
                if k.arg != "reader":
                    continue
                v = k.value
                ok = ((isinstance(v, ast.Call) and isinstance(v.func, ast.Name)
                       and v.func.id == "_ci_reader") or (isinstance(v, ast.Name) and v.id in bound))
                if not ok:
                    bad.append(n.lineno)
        self.assertEqual(bad, [])

    def test_the_failed_check_snapshot_reads_through_s0(self):
        seen = []

        def conclusion(repo, sha, required=None, run=None, reader=None):
            seen.append(reader)
            return ("green", "x")
        with mock.patch.object(L, "orphan_report"), mock.patch.object(L, "refresh_main"), \
             mock.patch.object(L, "world_checks", return_value=([], [])), \
             mock.patch.object(L, "_refuse_if_ci_red", return_value="absent"), \
             mock.patch.object(L, "static_checks",
                               return_value=([], None, {"result": "fail", "output": "x"})), \
             mock.patch.object(L, "_cwd_repo_from_git", return_value="o/r"), \
             mock.patch.object(L, "_ci_reader", return_value="SENTINEL"), \
             mock.patch.object(L.gate_ci, "ci_conclusion", side_effect=conclusion), \
             mock.patch.object(L, "_emit_attempt"), self.assertRaises(SystemExit):
            L._post_kind("o/r", 1, "ready-for-l3", "## r\n", SHA, "feat/1-x", attempt={})
        self.assertEqual(seen, ["SENTINEL"])


if __name__ == "__main__":
    unittest.main()
