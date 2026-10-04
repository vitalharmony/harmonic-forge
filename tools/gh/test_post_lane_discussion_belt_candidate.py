#!/usr/bin/env python3
"""harmonic-forge#691 (rescoped), AC1'/AC5'. `post_lane_discussion.py` is
the THIRD marker-posting tool -- the one the pre-rescope design missed,
despite it being the actual path Lane 2's own guard (`reject_plan_as_
discussion` in this same module) routes `plan` postings through. This
covers: `main()` actually calls the shared recorder on a successful post
(AC5'), `posted_by` is derived from `LANE` using `belt_candidates`'s own
`"l1"`/`"l2"`/`"l3"` convention (not the footer's `"LANE1"` spelling), and
a missing/unrecognized `LANE` records as `"unknown"` rather than being
misattributed to a real lane.
"""
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import post_lane_discussion as P  # noqa: E402

PLAN_BODY = "## Plan — H618\n\nDo the thing.\n"


class PostLaneDiscussionBeltCandidateIntegrationTests(unittest.TestCase):
    def _post(self, kind: str, body: str, lane: str | None) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "body.md"
            path.write_text(body)
            argv = ["post_lane_discussion.py", "--repo", "vitalharmony/harmonic-forge",
                    "--issue", "618", "--file", str(path), "--kind", kind]
            env = {"LANE": lane} if lane else {}
            with unittest.mock.patch.object(sys, "argv", argv), \
                 unittest.mock.patch.dict("os.environ", env, clear=not lane), \
                 unittest.mock.patch.object(
                     P, "comment_body",
                     return_value=("https://example/1", 1)), \
                 unittest.mock.patch.object(P, "belt_candidates") as recorder:
                P.main()
        return recorder

    def test_a_plan_posted_by_lane2_records_kind_plan_posted_by_l2(self) -> None:
        recorder = self._post("plan", PLAN_BODY, lane="2")
        recorder.record_candidate.assert_called_once_with(
            "vitalharmony/harmonic-forge", 618, "plan", "l2")

    def test_lane3_posting_a_spec_records_posted_by_l3(self) -> None:
        spec = ("## Lane 3 Test Spec — H618\n\n"
                "**Cases:** 1.\n**Next:** submit for HITL approval.\n\n"
                "### Test cases\n1. TC1 — a thing.\n")
        recorder = self._post("spec", spec, lane="3")
        recorder.record_candidate.assert_called_once_with(
            "vitalharmony/harmonic-forge", 618, "spec", "l3")

    def test_an_unset_lane_records_posted_by_unknown_not_a_real_lane(self) -> None:
        recorder = self._post("discussion", "just chatting", lane=None)
        recorder.record_candidate.assert_called_once_with(
            "vitalharmony/harmonic-forge", 618, "discussion", "unknown")

    def test_a_missing_platform_checkout_does_not_break_posting(self) -> None:
        """Same graceful-absence posture as `gate_ci` in this module: a
        sibling directory moving must never make posting fail."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "body.md"
            path.write_text("just chatting")
            argv = ["post_lane_discussion.py", "--issue", "618",
                    "--file", str(path), "--kind", "discussion"]
            with unittest.mock.patch.object(sys, "argv", argv), \
                 unittest.mock.patch.object(P, "belt_candidates", None), \
                 unittest.mock.patch.object(
                     P, "comment_body", return_value=("https://example/1", 1)):
                P.main()  # must not raise


class BeltCandidateFailedImportIsVisibleTests(unittest.TestCase):
    """harmonic-forge#691 preclose finding 4. A bare `except ImportError:
    belt_candidates = None` with `record_candidate` never even attempted
    used to be a completely silent no-op -- the `[post-comment] posted...`
    success line still printed, so "recorded" and "silently recorded
    nothing" were indistinguishable. Confirmed live: this repo's branch
    cannot currently import harmonic-forge's `belt_candidates` (it lives
    only on harmonic-forge's `feat/691-belt-candidate-reader` branch, not
    yet `main`), so every post through this path records nothing today,
    with zero indication anything is wrong. The fix does not change
    behavior -- it makes the existing no-op audible on stderr."""

    def test_a_missing_platform_checkout_prints_a_stderr_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "body.md"
            path.write_text("just chatting")
            argv = ["post_lane_discussion.py", "--repo", "vitalharmony/harmonic-forge",
                    "--issue", "618", "--file", str(path), "--kind", "discussion"]
            with unittest.mock.patch.object(sys, "argv", argv), \
                 unittest.mock.patch.object(P, "belt_candidates", None), \
                 unittest.mock.patch.object(
                     P, "comment_body", return_value=("https://example/1", 1)), \
                 unittest.mock.patch.object(sys, "stderr", new=__import__("io").StringIO()) as fake_err:
                P.main()  # must not raise -- the marker comment itself still succeeds
            warning = fake_err.getvalue()
        self.assertIn("belt-candidate not recorded", warning)
        self.assertIn("vitalharmony/harmonic-forge#618", warning)

    def test_a_present_platform_checkout_prints_no_warning(self) -> None:
        """The inverse: when recording DOES happen, this specific warning
        must not fire -- a warning on every post would be as useless as
        none at all."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "body.md"
            path.write_text("just chatting")
            argv = ["post_lane_discussion.py", "--repo", "vitalharmony/harmonic-forge",
                    "--issue", "618", "--file", str(path), "--kind", "discussion"]
            with unittest.mock.patch.object(sys, "argv", argv), \
                 unittest.mock.patch.object(P, "belt_candidates") as recorder, \
                 unittest.mock.patch.object(
                     P, "comment_body", return_value=("https://example/1", 1)), \
                 unittest.mock.patch.object(sys, "stderr", new=__import__("io").StringIO()) as fake_err:
                P.main()
            warning = fake_err.getvalue()
        recorder.record_candidate.assert_called_once()
        self.assertNotIn("belt-candidate not recorded", warning)


if __name__ == "__main__":
    unittest.main()
