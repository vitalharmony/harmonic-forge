import unittest
from unittest import mock

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


class MarkerFieldParsingTests(unittest.TestCase):
    """harmonic-forge#745 AC1/TC4."""

    def test_full_marker_with_timing_parses_every_field(self):
        body = (
            "<!-- lane-pr-link v1; issue-repo=o/r; issue=1; issue-node-id=X; "
            "pr-repo=o/r; pr=2; pr-node-id=Y; head-sha=abc123; "
            "local-check-start=2026-01-01T00:00:00+00:00; "
            "local-check-end=2026-01-01T00:05:00+00:00; ci-check-name=verify; "
            "ci-snapshot-state=pending; ci-snapshot-at=2026-01-01T00:05:00+00:00 -->"
        )
        fields = report._parse_marker_fields(body)
        self.assertEqual(fields["ci-snapshot-state"], "pending")
        self.assertEqual(fields["local-check-start"], "2026-01-01T00:00:00+00:00")

    def test_pre_745_marker_with_no_timing_fields_still_parses(self):
        """TC4: an old marker is parseable and its timing fields are simply
        absent -- never fabricated."""
        body = "<!-- lane-pr-link v1; issue-repo=o/r; issue=1; pr-repo=o/r; pr=2; head-sha=abc123 -->"
        fields = report._parse_marker_fields(body)
        self.assertEqual(fields["head-sha"], "abc123")
        self.assertNotIn("ci-snapshot-state", fields)

    def test_no_marker_returns_none(self):
        self.assertIsNone(report._parse_marker_fields("just a discussion comment"))


class MarkerOverlapTests(unittest.TestCase):
    """harmonic-forge#745 AC2/AC3."""

    def _comment(self, state="pending", with_timing=True):
        timing = (
            "local-check-start=2026-01-01T00:00:00+00:00; "
            "local-check-end=2026-01-01T00:05:00+00:00; ci-check-name=verify; "
            f"ci-snapshot-state={state}; ci-snapshot-at=2026-01-01T00:05:00+00:00;"
            if with_timing else ""
        )
        return {"body": (
            f"<!-- lane-pr-link v1; issue-repo=o/r; issue=1; pr-repo=o/r; pr=2; "
            f"head-sha=abc123;{timing} -->"
        )}

    def test_a_pre_745_marker_is_counted_unknown_not_fabricated(self):
        rows = report.marker_overlap([self._comment(with_timing=False)])
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["ci_snapshot_state"])
        self.assertIsNone(rows[0]["post_ready_remaining_ci_seconds"])

    def test_a_non_pending_snapshot_is_not_re_queried(self):
        with mock.patch.object(report.gate_ci, "ci_conclusion") as m:
            rows = report.marker_overlap([self._comment(state="green")])
        m.assert_not_called()
        self.assertEqual(rows[0]["ci_snapshot_state"], "green")
        self.assertIsNone(rows[0]["post_ready_remaining_ci_seconds"])

    def test_a_pending_snapshot_is_re_queried_exactly_once(self):
        with mock.patch.object(report.gate_ci, "ci_conclusion", return_value=("green", "ok")) as m, \
             mock.patch.object(report, "_raw_check_run",
                                return_value={"completed_at": "2026-01-01T00:10:00+00:00"}):
            rows = report.marker_overlap([self._comment(state="pending")])
        m.assert_called_once_with("o/r", "abc123", required={"verify"})
        self.assertEqual(rows[0]["final_ci_state"], "green")
        self.assertEqual(rows[0]["post_ready_remaining_ci_seconds"], 300.0)

    def test_a_still_pending_re_query_leaves_remaining_time_unresolved(self):
        """No wait, no retry -- if the re-query still says pending, that's
        the reported state, not a reason to poll again."""
        with mock.patch.object(report.gate_ci, "ci_conclusion", return_value=("pending", "still running")):
            rows = report.marker_overlap([self._comment(state="pending")])
        self.assertEqual(rows[0]["final_ci_state"], "pending")
        self.assertIsNone(rows[0]["post_ready_remaining_ci_seconds"])

    def test_no_lane_pr_link_marker_yields_no_rows(self):
        self.assertEqual(report.marker_overlap([{"body": "plain discussion"}]), [])
