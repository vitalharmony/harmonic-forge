#!/usr/bin/env python3
"""harmonic-forge#1017: every fast-stage validator runs and l1-post refuses once,
naming every failure; a single failure keeps today's exact text."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import l1_post  # noqa: E402

REPO, ISSUE = "vitalharmony/harmonic-forge", 35
NO_LEAD = "## Rework — F35\n\nbody with no summary block\n"


def gate(cid, verdict):
    return {"id": cid, "body": (f"## Lane 3 Gate Results — F35\n\n**Verdict:** {verdict}\n"
                                "**Finding:** x\n**Next:** y\n\nHead-SHA: abc1234\n\n"
                                "<!-- l1-post v1; kind=gate-result; posted-by=LANE3 -->")}


class CollectTests(unittest.TestCase):
    def test_single_failure_text_unchanged(self):
        with self.assertRaises(SystemExit) as direct:
            l1_post.fail("one problem")
        with self.assertRaises(SystemExit) as collected:
            l1_post._collect([lambda: None, lambda: l1_post.fail("one problem")])
        self.assertEqual(str(collected.exception), str(direct.exception))

    def test_every_step_runs_and_failures_are_numbered(self):
        ran = []

        def step(name, bad):
            def run():
                ran.append(name)
                if bad:
                    l1_post.fail(f"{name} is wrong")
            return run

        with self.assertRaises(SystemExit) as caught:
            l1_post._collect([step("a", True), step("b", False), step("c", True)])
        self.assertEqual(ran, ["a", "b", "c"])
        self.assertEqual(str(caught.exception),
                         "[l1-post] 2 problems:\n1. a is wrong\n2. c is wrong")

    def test_an_unrelated_exception_propagates(self):
        with self.assertRaises(ValueError):
            l1_post._collect([lambda: l1_post.fail("x"), lambda: (_ for _ in ()).throw(ValueError("y"))])

    def test_a_foreign_system_exit_propagates(self):
        with self.assertRaises(SystemExit) as caught:
            l1_post._collect([lambda: sys.exit(3)])
        self.assertEqual(caught.exception.code, 3)


class MainCollectTests(unittest.TestCase):
    def _main(self, kind, body, comments=(), extra=()):
        argv = ["l1_post.py", "--repo", REPO, "--issue", str(ISSUE), "--kind", kind,
                "--sha", "deadbeef", "--branch", "feat/35-x", "--file", "/dev/null"]
        if kind == "handoff":
            argv += ["--plan-first", "false"]
        result = mock.Mock(returncode=0, stdout=json.dumps(list(comments)), stderr="")
        patches = [mock.patch.object(sys, "argv", argv),
                   mock.patch.object(l1_post, "resolve_sha", return_value="deadbeef"),
                   mock.patch.object(l1_post, "regular_body", return_value=body),
                   mock.patch.object(l1_post, "reject_reserved_marker"),
                   mock.patch.object(l1_post, "run", return_value=result),
                   mock.patch.object(l1_post, "record_queue_candidate"),
                   mock.patch.object(l1_post, "apply_project_identity"),
                   mock.patch.object(l1_post, "resolve_repo", side_effect=lambda value, *a, **k: value),
                   mock.patch.object(l1_post, "validate_milestone_current"),
                   *extra]
        post = mock.patch.object(l1_post, "post_kind", return_value=("https://example/1", 1))
        for patch in [*patches, post]:
            patch.start()
            self.addCleanup(patch.stop)
        with self.assertRaises(SystemExit) as caught:
            l1_post.main()
        # A refusal posts nothing: post_kind (which runs static_checks) is never reached.
        l1_post.post_kind.assert_not_called()
        return str(caught.exception)

    def test_milestone_ack_reaches_posted_body(self):
        """The main() -> post_kind hop carries --milestone-ack (harmonic-forge#1033)."""
        argv = ["l1_post.py", "--repo", REPO, "--issue", str(ISSUE), "--kind", "rework",
                "--sha", "deadbeef", "--branch", "feat/35-x", "--file", "/dev/null",
                "--milestone-ack", "queued by operator"]
        patches = [mock.patch.object(sys, "argv", argv),
                   mock.patch.object(l1_post, "resolve_sha", return_value="deadbeef"),
                   mock.patch.object(l1_post, "regular_body", return_value="body"),
                   mock.patch.object(l1_post, "reject_reserved_marker"),
                   mock.patch.object(l1_post, "record_queue_candidate"),
                   mock.patch.object(l1_post, "apply_project_identity"),
                   mock.patch.object(l1_post, "resolve_repo", side_effect=lambda value, *a, **k: value),
                   mock.patch.object(l1_post, "_collect")]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        with mock.patch.object(l1_post, "post_kind", return_value=("https://example/1", 1)) as post:
            l1_post.main()
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["milestone_ack"], "queued by operator")

    def test_two_defects_one_refusal(self):
        """A rework with no lead block, after two FAILs with no sticky-wicket receipt."""
        with mock.patch("sticky_receipt.covered", return_value=False):
            message = self._main("rework", NO_LEAD, [gate(1, "FAIL"), gate(2, "FAIL")])
        self.assertTrue(message.startswith("[l1-post] 2 problems:\n1. "), message)
        self.assertIn("sticky-wicket is owed", message)
        self.assertIn("must lead with its summary block", message)

    def test_body_and_board_failure_reported_together(self):
        board = mock.patch.object(l1_post, "validate_board_fields_set",
                                  side_effect=lambda *a: l1_post.fail("Tier is not set on the board"))
        quiet = [mock.patch.object(l1_post, name) for name in (
            "validate_milestone_set", "validate_tooling_exception_labelled",
            "validate_pitch_inspected", "validate_sticky_wicket")]
        message = self._main("handoff", "## Handoff: F35\n\nno sections\n", extra=[board, *quiet])
        self.assertIn("problems:", message)
        self.assertIn("Tier is not set on the board", message)
        self.assertIn("handoff", message.split("Tier is not set")[0])

    def test_one_defect_keeps_todays_text(self):
        message = self._main("rework", NO_LEAD)
        with self.assertRaises(SystemExit) as direct:
            l1_post.validate_lead("rework", NO_LEAD)
        self.assertEqual(message, str(direct.exception))


#: The fast-stage validators per kind, in the order `main()` ran them before
#: harmonic-forge#1017 (AC2: none added, removed or reordered).
ORDER = {
    "handoff": ["validate_handoff", "validate_plan_first_declared", "validate_plan_first_spec",
                "validate_board_fields_set", "validate_milestone_set",
                "validate_milestone_current", "validate_tooling_exception_labelled", "validate_pitch_inspected",
                "validate_sticky_wicket", "validate_lead"],
    "rework": ["validate_milestone_current", "validate_sticky_wicket", "validate_lead"],
    "ready-for-l3": ["validate_sticky_wicket", "validate_lead"],
    "sweep": ["validate_sticky_wicket", "validate_lead", "_spec_body", "validate_sweep",
              "validate_sweep_against_posted_ae"],  # harmonic-forge#1115 AC9
    "ae": ["validate_sticky_wicket", "validate_lead", "validate_ae", "validate_grant_ae",
           "validate_auto_ae", "_latest_sweep_calls", "validate_ae_external_calls"],
    "ae-and-sweep": ["validate_sticky_wicket", "validate_ae", "validate_grant_ae",
                     "validate_auto_ae", "validate_lead", "validate_lead", "_spec_body",
                     "validate_sweep", "validate_ae_external_calls"],
}


class ValidatorOrderTests(unittest.TestCase):
    """AC2: every kind runs the same validators, in the same order, as before."""

    def _calls(self, kind):
        calls = []
        argv = ["l1_post.py", "--repo", REPO, "--issue", str(ISSUE), "--kind", kind,
                "--sha", "deadbeef", "--branch", "feat/35-x", "--spec-comment", "9"]
        argv += (["--ae-file", "/dev/null", "--sweep-file", "/dev/null"] if kind == "ae-and-sweep"
                 else ["--file", "/dev/null"])
        if kind == "handoff":
            argv += ["--plan-first", "false"]
        if kind not in ("sweep", "ae-and-sweep"):
            argv.remove("--spec-comment"), argv.remove("9")
        patches = [mock.patch.object(sys, "argv", argv),
                   mock.patch.object(l1_post, "resolve_sha", return_value="deadbeef"),
                   mock.patch.object(l1_post, "regular_body", return_value="body"),
                   mock.patch.object(l1_post, "reject_reserved_marker"),
                   mock.patch.object(l1_post, "apply_project_identity"),
                   mock.patch.object(l1_post, "resolve_repo", side_effect=lambda value, *a, **k: value),
                   mock.patch.object(l1_post, "grant_on_main", return_value=False),
                   mock.patch.object(l1_post, "post_kind", return_value=("https://example/1", 1)),
                   mock.patch.object(l1_post, "record_queue_candidate")]
        for name in {n for names in ORDER.values() for n in names}:
            patches.append(mock.patch.object(
                l1_post, name, side_effect=lambda *a, _n=name, **k: calls.append(_n) or "spec"))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        l1_post.main()
        return calls

    def test_each_kind_runs_its_validators_in_order(self):
        for kind, expected in ORDER.items():
            with self.subTest(kind=kind):
                self.assertEqual(self._calls(kind), expected)



# harmonic-forge#1115: these tests drive l1_post, not the dry-run preflight
# gate (test_l1_post_dry_run.py owns that). With no session id the gate does
# not apply, so the outcome cannot depend on the caller's
# CLAUDE_CODE_SESSION_ID (set in any Claude session and in kill_check runs).
# Child processes inherit the stripped environment.
import os as _os  # noqa: E402
from unittest import mock as _session_mock  # noqa: E402

_NO_SESSION_PATCH = _session_mock.patch.dict(_os.environ)


def setUpModule():  # noqa: N802
    _NO_SESSION_PATCH.start()
    _os.environ.pop("CLAUDE_CODE_SESSION_ID", None)


def tearDownModule():  # noqa: N802
    _NO_SESSION_PATCH.stop()


if __name__ == "__main__":
    unittest.main()
