#!/usr/bin/env python3
"""harmonic-forge#1103 AC3. A Lane 3 gate report posted through
`post_lane_discussion.py` under a non-`gate-result` footer kind (the
`discussion` default, a `finding`, or no `--kind` at all) used to be recorded
in the belt candidate store under that footer kind, which
`belt_candidates.read_candidates` drops for every lane -- so the FAIL reached
no lane. It is now recorded as `gate-result`, using the same recognizer
(`watch_lane_posts._is_gate_report`) both belt channels use."""
import datetime as dt
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import belt_candidates  # noqa: E402
import post_lane_discussion as P  # noqa: E402
import watch_lane_posts  # noqa: E402

REPO = "vitalharmony/harmonic-forge"
FAIL_BODY = ("## Lane 3 Gate Results — F1103\n\n"
             "**Verdict:** FAIL\n**Head-SHA:** " + "a" * 40 + "\n\nTC1 failed.\n")
QUOTED_BODY = ("Relaying what Lane 3 said:\n\n"
               "> ## Lane 3 Gate Results — F1103\n> **Verdict:** FAIL\n")


class CandidateKindTests(unittest.TestCase):
    def test_a_discussion_footer_gate_report_records_gate_result(self) -> None:
        self.assertEqual(P.candidate_kind("discussion", FAIL_BODY), "gate-result")

    def test_a_finding_footer_gate_report_records_gate_result(self) -> None:
        self.assertEqual(P.candidate_kind("finding", FAIL_BODY), "gate-result")

    def test_no_footer_gate_report_records_gate_result(self) -> None:
        self.assertEqual(P.candidate_kind("", FAIL_BODY), "gate-result")

    def test_a_quoted_gate_heading_is_not_a_gate_report(self) -> None:
        self.assertEqual(P.candidate_kind("discussion", QUOTED_BODY), "discussion")

    def test_a_prefaced_gate_report_records_gate_result(self) -> None:
        """harmonic-forge#1103 preclose survivor 3: prose above the heading."""
        self.assertEqual(P.candidate_kind("discussion", "Gate done, details below.\n\n" + FAIL_BODY),
                         "gate-result")

    def test_a_tilde_fenced_gate_heading_is_not_a_gate_report(self) -> None:
        """harmonic-forge#1103 preclose pass 2: `~~~` fences are stripped."""
        body = "Example of a report:\n\n~~~~markdown\n" + FAIL_BODY + "~~~~\n"
        self.assertEqual(P.candidate_kind("discussion", body), "discussion")

    def test_an_unclosed_or_shorter_closed_fence_hides_the_heading(self) -> None:
        """harmonic-forge#1103 preclose pass 3: an unclosed `~~~` fence runs
        to the end; a ```` fence is not closed by a ``` line inside it."""
        unclosed = "Example:\n\n~~~\n" + FAIL_BODY
        self.assertEqual(P.candidate_kind("discussion", unclosed), "discussion")
        nested = "Example:\n\n````\n```\n" + FAIL_BODY + "````\n"
        self.assertEqual(P.candidate_kind("discussion", nested), "discussion")


class GateReportReachesLane1Tests(unittest.TestCase):
    """End to end through `main()` and the real candidate store."""

    def _post(self, body: str, base: Path, kind: str | None) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "body.md"
            path.write_text(body)
            argv = ["post_lane_discussion.py", "--repo", REPO,
                    "--issue", "1103", "--file", str(path)]
            if kind is not None:
                argv += ["--kind", kind]
            real = belt_candidates.record_candidate

            def record(*a, **kw):
                return real(*a, base_dir=base, **kw)

            with unittest.mock.patch.object(sys, "argv", argv), \
                 unittest.mock.patch.dict("os.environ", {"LANE": "3"}), \
                 unittest.mock.patch.object(P, "require_green_ci", return_value=False), \
                 unittest.mock.patch.object(P, "require_round_approval"), \
                 unittest.mock.patch.object(
                     P, "comment_body", return_value=("https://example/1", 1)), \
                 unittest.mock.patch.object(P.belt_candidates, "record_candidate",
                                            side_effect=record):
                P.main()

    def _l1_candidates(self, base: Path) -> set:
        return watch_lane_posts.read_queue_candidates(
            [REPO], "l1", now=dt.datetime.now(dt.timezone.utc), base_dir=base)

    def test_a_discussion_footered_fail_is_a_lane1_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._post(FAIL_BODY, base, "discussion")
            self.assertIn((REPO, 1103), self._l1_candidates(base))

    def test_a_default_kind_fail_is_a_lane1_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._post(FAIL_BODY, base, None)
            self.assertIn((REPO, 1103), self._l1_candidates(base))



class UnguardedImportTests(unittest.TestCase):
    def test_a_missing_watch_lane_posts_fails_the_import_loudly(self) -> None:
        """harmonic-forge#1103 preclose pass 2 (R-0381): no optional guard."""
        import importlib
        saved = sys.modules.pop("post_lane_discussion")
        try:
            with unittest.mock.patch.dict(sys.modules, {"watch_lane_posts": None}):
                with self.assertRaises(ImportError):
                    importlib.import_module("post_lane_discussion")
        finally:
            sys.modules["post_lane_discussion"] = saved


if __name__ == "__main__":
    unittest.main()
