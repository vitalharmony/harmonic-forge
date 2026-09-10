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

    def test_ansi_only_mismatch_names_itself_a_transit_mangled_escape(self):
        """harmonic-forge#571 AC3. The real live shape: a vite-reporter
        fragment survives round-trip except for its ANSI escape, and the
        failure must say so -- not send the reader toward the disproven
        "GitHub strips characters" hypothesis."""
        sent = "narrative with \x1b[33m[INEFFECTIVE_DYNAMIC_IMPORT]\x1b[39m color"
        # Same visible content, differently-coded escapes -- stripped, both
        # read identically; only the raw escape bytes were mangled in transit.
        landed = "narrative with \x1b[1;33m[INEFFECTIVE_DYNAMIC_IMPORT]\x1b[0m color"
        self.assertEqual(rr.strip_ansi(sent), rr.strip_ansi(landed))
        self.assertNotEqual(sent, landed)
        posted = {"id": 558, "html_url": "https://example/558"}
        with unittest.mock.patch.object(lp, "_gh_api") as fake:
            fake.side_effect = [
                _fake_run(0, stdout=json.dumps(posted)),
                _fake_run(0, stdout=json.dumps({"body": landed})),
            ]
            with self.assertRaises(SystemExit) as ctx:
                lp.post("o/r", 1, sent)
        message = str(ctx.exception)
        self.assertIn("transit-mangled escape", message)
        self.assertNotIn("GitHub strips characters", message)

    def test_the_real_reported_shape_json_textual_escape_vs_raw_byte(self):
        """harmonic-forge#571 preclose finding: the live incident's SENT side
        never carries a raw ESC byte at all -- `compose_body`'s
        `json.dumps(ensure_ascii=True)` writes an embedded control byte as
        its 6-character textual escape (backslash, u, 0, 0, 1, b). Only the
        LANDED side (after transit-mangling) carries a real byte. A bare
        `strip_ansi(a) == strip_ansi(b)` comparison never equates these two
        representations; `_normalize_for_diagnosis` must."""
        receipts = [{"stdout_preview": "warn: \x1b[33mcolor\x1b[39m"}]
        sent = lp.compose_body("completion", receipts, "n",
                               {"Status": "s", "Change": "c", "Next": "n"})
        self.assertNotIn("\x1b", sent)  # json.dumps escaped it textually
        self.assertIn("u001b", sent.replace("\\", ""))  # the textual escape is present
        # Simulate transit mangling turning the textual escape into a real
        # control byte on the landed side, content otherwise identical.
        landed = sent.replace("\\u001b", "\x1b")
        self.assertNotEqual(sent, landed)
        self.assertEqual(rr.strip_ansi(sent), sent)  # nothing to strip on sent
        self.assertNotEqual(rr.strip_ansi(sent), rr.strip_ansi(landed))  # bare strip_ansi misses it
        self.assertEqual(lp._normalize_for_diagnosis(sent), lp._normalize_for_diagnosis(landed))
        posted = {"id": 561, "html_url": "https://example/561"}
        with unittest.mock.patch.object(lp, "_gh_api") as fake:
            fake.side_effect = [
                _fake_run(0, stdout=json.dumps(posted)),
                _fake_run(0, stdout=json.dumps({"body": landed})),
            ]
            with self.assertRaises(SystemExit) as ctx:
                lp.post("o/r", 1, sent)
        message = str(ctx.exception)
        self.assertIn("transit-mangled escape", message)
        self.assertNotIn("GitHub strips characters", message)

    def test_a_real_content_difference_is_not_misdiagnosed_as_ansi(self):
        """The AC3 distinction must cut both ways: a genuine content
        mismatch (no ANSI involved at all) must not be reported as a
        transit artifact."""
        posted = {"id": 559, "html_url": "https://example/559"}
        with unittest.mock.patch.object(lp, "_gh_api") as fake:
            fake.side_effect = [
                _fake_run(0, stdout=json.dumps(posted)),
                _fake_run(0, stdout=json.dumps({"body": "something else entirely"})),
            ]
            with self.assertRaises(SystemExit) as ctx:
                lp.post("o/r", 1, "hello")
        message = str(ctx.exception)
        self.assertNotIn("transit-mangled escape", message)
        self.assertIn("not an ANSI-escape artifact", message)

    def test_a_real_captured_vite_fragment_round_trips_after_receipt_runner_strips_it(self):
        """harmonic-forge#571 AC2. Regression using a real captured
        fragment (not a synthetic string that happens to dodge the case):
        once `receipt_runner.write_receipt`'s preview has gone through
        `strip_ansi`, composing and posting it must round-trip
        byte-identically even though the raw captured text carried color."""
        raw_vite_output = (
            "vite v5.4.0 building for production...\n"
            "\x1b[33m[INEFFECTIVE_DYNAMIC_IMPORT]\x1b[39m /src/app.ts is "
            "dynamically imported but also statically imported\n"
            "\x1b[32m✓ built in 842ms\x1b[39m\n"
        )
        stripped_preview = rr.strip_ansi(raw_vite_output)
        self.assertNotIn("\x1b", stripped_preview)
        receipts = [{"argv": ["npm", "run", "build"], "exit_code": 0,
                    "stdout_preview": stripped_preview}]
        body = lp.compose_body("completion", receipts, "build succeeded",
                               {"Status": "done", "Change": "built", "Next": "none"})
        self.assertNotIn("\x1b", body)
        posted = {"id": 560, "html_url": "https://example/560"}
        with unittest.mock.patch.object(lp, "_gh_api") as fake:
            fake.side_effect = [
                _fake_run(0, stdout=json.dumps(posted)),
                _fake_run(0, stdout=json.dumps({"body": body})),
            ]
            result = lp.post("o/r", 1, body)  # must not raise
        self.assertEqual(result["comment_id"], 560)


class TestFindingKind(unittest.TestCase):
    """harmonic-forge#571 AC4/AC5 -- a durable, attributed way for Lane 2 to
    post a defect note, distinct from a status transition."""

    def test_finding_heading_does_not_match_the_l2_status_pattern(self):
        """`watch_lane_posts.py`'s `_L2_HEADING_RE` and HRSE2's
        `lane_state.py`'s `_LANE_TOKEN` both key on `^##\\s+L2[A-Z]\\b` --
        `L2F` would satisfy that (F is an allowed status code), so the
        heading must not take that shape."""
        body = lp.compose_body("finding", [], "diagnosed a defect")
        heading = body.splitlines()[0]
        self.assertNotRegex(heading, r"^##\s+L2[A-Z]\b")
        self.assertIn("L2 Finding", heading)

    def test_finding_heading_is_visible_to_the_belt_classifier(self):
        """harmonic-forge#571 preclose finding: AC5 requires NOT being read
        as a status transition, not invisibility. Without belt visibility a
        finding is still an operator-relay-only artifact -- the exact defect
        AC4 exists to close. `watch_lane_posts._classify` must still tag it
        as an `l2` event."""
        import watch_lane_posts as wlp
        body = lp.compose_body("finding", [], "diagnosed a defect")
        classified = wlp._classify(body)
        self.assertIsNotNone(classified)
        self.assertEqual(classified[0], "l2")

    def test_narrative_ansi_is_stripped_by_compose_body(self):
        """harmonic-forge#571 preclose finding: the narrative is free text,
        embedded unescaped (not through json.dumps like the receipts JSON)
        -- the likeliest place a `--kind finding` post carries colour, since
        a finding often quotes the failing command's own output."""
        narrative = "I found: \x1b[33m[INEFFECTIVE_DYNAMIC_IMPORT]\x1b[39m in the build log"
        body = lp.compose_body("finding", [], narrative)
        self.assertNotIn("\x1b", body)
        self.assertIn("[INEFFECTIVE_DYNAMIC_IMPORT]", body)

    def test_finding_is_not_required_to_carry_a_lead_block(self):
        """AC5: a finding is a report, not a status transition -- it must
        not be forced through the completion/blocked lead-block gate."""
        lp.validate_lead("finding", {})  # must not raise

    def test_finding_bypasses_the_issue_lock_like_blocked_does(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            subprocess.run(["git", "init", "-q"], cwd=tmp.name, check=True)
            import os
            cwd = Path.cwd()
            os.chdir(tmp.name)
            try:
                rr.write_lock(9200, rr.receipt_dir(9200) / "x.json", 1)
                self.assertTrue(rr.is_locked(9200))
                self.assertFalse(lp.lock_blocks("finding", 9200))
                self.assertFalse(lp.lock_blocks("blocked", 9200))
                self.assertTrue(lp.lock_blocks("completion", 9200))
                self.assertTrue(lp.lock_blocks("plan", 9200))
            finally:
                os.chdir(cwd)
        finally:
            tmp.cleanup()

    def test_unlocked_issue_never_blocks_any_kind(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            subprocess.run(["git", "init", "-q"], cwd=tmp.name, check=True)
            import os
            cwd = Path.cwd()
            os.chdir(tmp.name)
            try:
                self.assertFalse(lp.lock_blocks("completion", 9201))
            finally:
                os.chdir(cwd)
        finally:
            tmp.cleanup()


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
