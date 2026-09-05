#!/usr/bin/env python3
"""Unit tests for l2_post.py (harmonic-forge#371).

Run: python3 tools/gh/test_l2_post.py
"""
import json
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import l2_post as lp
import receipt_runner as rr


def _fake_run(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestComposeBody(unittest.TestCase):
    def test_labels_map_correctly(self):
        for kind, label in (("plan", "L2P"), ("completion", "L2D"), ("blocked", "L2B")):
            body = lp.compose_body(kind, [], "narrative text")
            self.assertIn(label, body)
            self.assertIn("narrative text", body)
            self.assertIn("Verified receipts", body)

    def test_receipts_embedded_as_json(self):
        receipts = [{"argv": ["echo", "hi"], "exit_code": 0}]
        body = lp.compose_body("completion", receipts, "n")
        self.assertIn('"echo"', body)
        self.assertIn('"exit_code": 0', body)


class TestPostSelfCheck(unittest.TestCase):
    def test_post_succeeds_when_refetch_matches(self):
        posted = {"id": 555, "html_url": "https://example/555"}
        refetched = {"body": "hello"}
        with unittest.mock.patch.object(lp, "_gh_api") as fake:
            fake.side_effect = [
                _fake_run(0, stdout=json.dumps(posted)),
                _fake_run(0, stdout=json.dumps(refetched)),
            ]
            result = lp.post("o/r", 1, "hello")
        self.assertEqual(result["comment_id"], 555)
        self.assertEqual(result["body_sha256"], lp._sha("hello"))

    def test_post_refuses_success_on_body_mismatch(self):
        """The core integrity property this issue exists for: a landed
        comment whose body doesn't match what was sent must never be
        reported as a success."""
        posted = {"id": 556, "html_url": "https://example/556"}
        refetched = {"body": "something else entirely"}
        with unittest.mock.patch.object(lp, "_gh_api") as fake:
            fake.side_effect = [
                _fake_run(0, stdout=json.dumps(posted)),
                _fake_run(0, stdout=json.dumps(refetched)),
            ]
            with self.assertRaises(SystemExit):
                lp.post("o/r", 1, "hello")

    def test_post_fails_when_initial_post_transport_fails(self):
        with unittest.mock.patch.object(lp, "_gh_api") as fake:
            fake.return_value = _fake_run(1, stderr="network error")
            with self.assertRaises(SystemExit):
                lp.post("o/r", 1, "hello")

    def test_post_fails_when_refetch_itself_fails(self):
        posted = {"id": 557, "html_url": "https://example/557"}
        with unittest.mock.patch.object(lp, "_gh_api") as fake:
            fake.side_effect = [
                _fake_run(0, stdout=json.dumps(posted)),
                _fake_run(1, stderr="not found"),
            ]
            with self.assertRaises(SystemExit):
                lp.post("o/r", 1, "hello")


class TestResolveLock(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        subprocess.run(["git", "init", "-q"], cwd=self._tmp.name, check=True)
        import os
        self._cwd = Path.cwd()
        os.chdir(self._tmp.name)

    def tearDown(self):
        import os
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_resolve_lock_refused_without_fetchable_comment(self):
        rr.run_command(4242, ["false"])
        self.assertTrue(rr.is_locked(4242))
        with unittest.mock.patch.object(lp, "_gh_api") as fake:
            fake.return_value = _fake_run(1, stderr="404")
            with self.assertRaises(SystemExit):
                lp.resolve_lock("o/r", 4242, 999)
        self.assertTrue(rr.is_locked(4242))

    def test_resolve_lock_clears_when_comment_is_real(self):
        rr.run_command(4243, ["false"])
        self.assertTrue(rr.is_locked(4243))
        with unittest.mock.patch.object(lp, "_gh_api") as fake:
            fake.return_value = _fake_run(0, stdout='{"id": 999, "body": "resolved"}')
            lp.resolve_lock("o/r", 4243, 999)
        self.assertFalse(rr.is_locked(4243))


class TestSnapshot(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        subprocess.run(["git", "init", "-q"], cwd=self._tmp.name, check=True)
        import os
        self._cwd = Path.cwd()
        os.chdir(self._tmp.name)

    def tearDown(self):
        import os
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_snapshot_records_comment_ids_and_body_hashes(self):
        comments = [{"id": 1, "body": "a"}, {"id": 2, "body": "b"}]
        raw = json.dumps(comments)
        with unittest.mock.patch.object(lp, "_gh_api") as fake:
            fake.return_value = _fake_run(0, stdout=raw)
            body = lp.snapshot("o/r", 7001)
        self.assertEqual(body["comment_ids"], [1, 2])
        self.assertEqual(body["comment_body_sha256"]["1"], lp._sha("a"))
        self.assertEqual(body["raw_response_sha256"], lp._sha(raw))


if __name__ == "__main__":
    unittest.main()


class TestLeadBlock(unittest.TestCase):
    """harmonic-forge#472 — outcome first, evidence collapsed.

    The emitter, not a convention: AC4 rejects prose asking a lane to
    remember, so these assert the shape a lane physically cannot avoid.
    """

    LEAD = {"Status": "implemented, unpushed", "Change": "three files",
            "Next": "L1 reviews"}

    def test_the_lead_precedes_the_narrative_and_the_evidence(self):
        """AC1. Positional, not merely present — `assertIn` on all three
        would pass on the old body, which led with the receipts."""
        body = lp.compose_body("completion", [{"exit_code": 0}], "long narrative",
                               self.LEAD)
        self.assertLess(body.index("**Status:**"), body.index("### Narrative"))
        self.assertLess(body.index("**Next:**"), body.index("<details>"))
        self.assertLess(body.index("### Narrative"), body.index("```json"))

    def test_receipts_are_retained_in_full_inside_the_details_block(self):
        """AC2. Nothing deleted, nothing moved to a second comment."""
        receipts = [{"argv": ["echo", "hi"], "exit_code": 0}]
        body = lp.compose_body("completion", receipts, "n", self.LEAD)
        opened = body.index("<details>")
        closed = body.index("</details>")
        self.assertIn('"echo"', body[opened:closed])
        self.assertIn('"exit_code": 0', body[opened:closed])
        self.assertIn("Verified receipts — 1", body)

    def test_a_reader_who_never_expands_still_has_the_outcome(self):
        """AC3, stated as the test the AC actually describes: strip the
        collapsed section and the outcome and next action survive."""
        body = lp.compose_body("completion", [{"x": 1}], "n", self.LEAD)
        visible = body[:body.index("<details>")]
        self.assertIn("implemented, unpushed", visible)
        self.assertIn("L1 reviews", visible)

    def test_the_marker_heading_stays_top_level(self):
        """AC6 on the issue: `lane_state.py` reads `## L2D` and hrse#1590
        made position load-bearing, so the heading may not move inside the
        collapsed block."""
        body = lp.compose_body("completion", [], "n", self.LEAD)
        self.assertTrue(body.startswith("## L2D "))
        self.assertLess(body.index("## L2D"), body.index("<details>"))

    def test_completion_and_blocked_refuse_a_missing_lead(self):
        for kind in ("completion", "blocked"):
            with self.subTest(kind=kind):
                with self.assertRaises(SystemExit) as ctx:
                    lp.validate_lead(kind, {"Status": "x", "Change": "", "Next": "y"})
                self.assertIn("--change", str(ctx.exception))

    def test_a_plan_may_omit_the_lead(self):
        """Question 2, answered as the plan's stated lean: a plan's finding
        IS the plan, and a mandatory one-line summary of what follows in full
        produces filler."""
        lp.validate_lead("plan", {})
        body = lp.compose_body("plan", [], "the plan", {})
        self.assertNotIn("**Status:**", body)
        self.assertIn("### Narrative", body)

    def test_an_empty_string_field_renders_no_line(self):
        """`main()` always passes all three keys — argparse defaults them to
        `""` — so a "was this key supplied" check would emit `**Status:**`
        with nothing after it on every plan post. The dict is never sparse in
        the real caller, which is why presence cannot be the test."""
        body = lp.compose_body("plan", [], "n",
                               {"Status": "", "Change": "", "Next": "review it"})
        self.assertNotIn("**Status:**", body)
        self.assertIn("**Next:** review it", body)

    def test_a_partial_lead_renders_only_what_was_given(self):
        body = lp.compose_body("plan", [], "n", {"Next": "review it"})
        self.assertIn("**Next:** review it", body)
        self.assertNotIn("**Status:**", body)

    def test_whitespace_only_lead_values_do_not_count_as_supplied(self):
        with self.assertRaises(SystemExit):
            lp.validate_lead("completion", {"Status": "  ", "Change": "c", "Next": "n"})
