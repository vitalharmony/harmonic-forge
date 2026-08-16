#!/usr/bin/env python3
"""Unit tests for fetch_lane1_context.py (harmonic-forge#253) -- all
subprocess calls mocked, no live gh/API calls.
Run: python3 tools/gh/test_fetch_lane1_context.py"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import fetch_lane1_context as f


def _completed(stdout="", returncode=0):
    class R:
        pass
    r = R()
    r.stdout = stdout
    r.stderr = ""
    r.returncode = returncode
    return r


class TestIsLane1Comment(unittest.TestCase):
    def test_handoff_kind_is_lane1(self):
        body = "some handoff text\n\n<!-- l1-post v1; kind=handoff; sha=abc; body-sha256=x; checks=y -->\n"
        self.assertTrue(f.is_lane1_comment(body))

    def test_ready_for_l3_kind_is_lane1(self):
        body = "ready\n\n<!-- l1-post v1; kind=ready-for-l3; sha=abc; body-sha256=x; checks=y -->\n"
        self.assertTrue(f.is_lane1_comment(body))

    def test_sweep_kind_is_lane1(self):
        body = "sweep\n\n<!-- l1-post v1; kind=sweep; sha=abc; body-sha256=x; checks=y -->\n"
        self.assertTrue(f.is_lane1_comment(body))

    def test_ae_kind_is_lane1(self):
        # hrse#327: a live Lane 3 Codex session's own filtered context
        # returned the sweep but not the AE comment, because "ae" was
        # missing from _LANE1_KINDS despite being a real l1-post kind
        # since hrse#929.
        body = "## AE\n\n<!-- l1-post v1; kind=ae; sha=abc; body-sha256=x; checks=y -->\n"
        self.assertTrue(f.is_lane1_comment(body))

    def test_discussion_posted_by_lane1_is_included(self):
        body = "note\n\n<!-- l1-post v1; kind=discussion; posted-by=LANE1 -->\n"
        self.assertTrue(f.is_lane1_comment(body))

    def test_discussion_posted_by_lane_unset_is_included(self):
        body = "note\n\n<!-- l1-post v1; kind=discussion; posted-by=LANE-unset -->\n"
        self.assertTrue(f.is_lane1_comment(body))

    def test_discussion_posted_by_lane2_is_excluded(self):
        body = "note\n\n<!-- l1-post v1; kind=discussion; posted-by=LANE2 -->\n"
        self.assertFalse(f.is_lane1_comment(body))

    def test_discussion_posted_by_lane3_is_excluded(self):
        body = "note\n\n<!-- l1-post v1; kind=discussion; posted-by=LANE3 -->\n"
        self.assertFalse(f.is_lane1_comment(body))

    def test_lane2_completion_report_with_no_marker_is_excluded(self):
        body = "L2 COMPLETE -- implemented and pushed fix/793-warm-path-bands..."
        self.assertFalse(f.is_lane1_comment(body))

    def test_lane3_blocked_report_with_no_marker_is_excluded(self):
        body = "L3 BLOCKED -- I cannot produce an independent test spec..."
        self.assertFalse(f.is_lane1_comment(body))

    def test_empty_body_is_excluded(self):
        self.assertFalse(f.is_lane1_comment(""))

    def test_lane1_comment_quoting_another_lanes_posted_by_is_included(self):
        """harmonic-forge#269 -- live incident on hrse#848: a genuine Lane 1
        discussion comment quoted Lane 3's footer tag by name in its prose
        ("the spec posted above (`posted-by=LANE3`, ...)") before its own
        real closing footer. The old unscoped regex matched the inline
        LANE3 mention instead of the marker's own posted-by field and
        misclassified a real Lane 1 comment as excluded."""
        body = (
            "the spec posted above (`posted-by=LANE3`, HITL Test Spec "
            "Review) is approved as written.\n\n"
            "<!-- l1-post v1; kind=discussion; posted-by=LANE1 -->\n"
        )
        self.assertTrue(f.is_lane1_comment(body))

    def test_lane2_comment_quoting_lane1_posted_by_is_still_excluded(self):
        """Same fix, opposite direction -- prose mentioning LANE1 must not
        launder a genuine Lane 2 comment into inclusion."""
        body = (
            "per Lane 1's comment (`posted-by=LANE1`), implemented as "
            "specified.\n\n"
            "<!-- l1-post v1; kind=discussion; posted-by=LANE2 -->\n"
        )
        self.assertFalse(f.is_lane1_comment(body))


class TestFetchComments(unittest.TestCase):
    def test_paginated_arrays_are_concatenated(self):
        """--paginate prints one JSON array per page back to back, not one
        combined array -- confirm the parser handles that shape, not just
        a single well-formed array."""
        page1 = json.dumps([{"body": "a"}, {"body": "b"}])
        page2 = json.dumps([{"body": "c"}])
        with patch("fetch_lane1_context._run", return_value=_completed(page1 + page2)):
            comments = f.fetch_comments("o/r", 1)
        self.assertEqual([c["body"] for c in comments], ["a", "b", "c"])

    def test_empty_response_returns_empty_list(self):
        with patch("fetch_lane1_context._run", return_value=_completed("")):
            comments = f.fetch_comments("o/r", 1)
        self.assertEqual(comments, [])

    def test_single_page_array(self):
        page = json.dumps([{"body": "only"}])
        with patch("fetch_lane1_context._run", return_value=_completed(page)):
            comments = f.fetch_comments("o/r", 1)
        self.assertEqual([c["body"] for c in comments], ["only"])

    def test_gh_failure_exits_nonzero(self):
        with patch("fetch_lane1_context._run", return_value=_completed(returncode=1)):
            with self.assertRaises(SystemExit) as ctx:
                f.fetch_comments("o/r", 1)
        self.assertEqual(ctx.exception.code, 1)


class TestFetchIssueBody(unittest.TestCase):
    def test_returns_stripped_body(self):
        with patch("fetch_lane1_context._run", return_value=_completed("some body\n")):
            body = f.fetch_issue_body("o/r", 1)
        self.assertEqual(body, "some body")

    def test_gh_failure_exits_nonzero(self):
        with patch("fetch_lane1_context._run", return_value=_completed(returncode=1)):
            with self.assertRaises(SystemExit) as ctx:
                f.fetch_issue_body("o/r", 1)
        self.assertEqual(ctx.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
