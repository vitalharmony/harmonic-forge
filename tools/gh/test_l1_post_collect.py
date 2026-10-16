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
                   mock.patch.object(l1_post, "post_kind", return_value=("https://example/1", 1)),
                   mock.patch.object(l1_post, "record_queue_candidate"), *extra]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        with self.assertRaises(SystemExit) as caught:
            l1_post.main()
        return str(caught.exception)

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
        self.assertNotIn("problems:", message)
        self.assertIn("must lead with its summary block", message)


if __name__ == "__main__":
    unittest.main()
