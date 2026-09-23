import unittest
import lane_transition_report as report


class TransitionTests(unittest.TestCase):
    def test_extracts_existing_marker_and_heading_transitions(self):
        comments = [
            {"created_at": "2026-01-01T00:00:00Z", "body": "<!-- l1-post v1; kind=handoff -->"},
            {"created_at": "2026-01-01T00:01:00Z", "body": "## L2D — done"},
            {"created_at": "2026-01-01T00:03:00Z", "body": "## Lane 3 Test Spec"},
        ]
        self.assertEqual(report.transitions(7, comments), [(7, "handoff->complete", 60.0), (7, "complete->spec", 120.0)])

    def test_ignores_unclassified_discussion(self):
        self.assertEqual(report.transitions(7, [{"created_at": "2026-01-01T00:00:00Z", "body": "hello"}]), [])
