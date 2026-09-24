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
    """harmonic-forge#745 AC2/AC3, rewritten for the preclose-inspection
    findings on @2892678: overlap must actually be calculated (not just
    parsed and dropped), zero-overlap observations must count toward the
    AC3 floor, an unresolved re-query must be its own explicit bucket, both
    new GitHub reads must go through `gh-as vitalharmony`, markers must
    dedup on `(pr-repo, head-sha)`, and a malformed timestamp must not
    crash the report."""

    def _comment(self, state="pending", with_timing=True, head_sha="abc123", pr=2):
        timing = (
            "local-check-start=2026-01-01T00:00:00+00:00; "
            "local-check-end=2026-01-01T00:05:00+00:00; ci-check-name=verify; "
            f"ci-snapshot-state={state}; ci-snapshot-at=2026-01-01T00:05:00+00:00;"
            if with_timing else ""
        )
        return {"body": (
            f"<!-- lane-pr-link v1; issue-repo=o/r; issue=1; pr-repo=o/r; pr={pr}; "
            f"head-sha={head_sha};{timing} -->"
        )}

    def test_a_pre_745_marker_is_counted_unknown_not_fabricated(self):
        rows = report.marker_overlap([self._comment(with_timing=False)])
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["ci_snapshot_state"])
        self.assertIsNone(rows[0]["post_ready_remaining_ci_seconds"])

    def test_a_green_at_ready_snapshot_is_still_re_queried_and_can_resolve_to_zero_overlap(self):
        """Preclose finding 2: excluding already-resolved-at-ready markers
        from the re-query made "retain the current flow" unreachable --
        this is exactly the zero-overlap case AC3 needs counted."""
        with mock.patch.object(report.gate_ci, "ci_conclusion", return_value=("green", "ok")), \
             mock.patch.object(report, "_raw_check_run", return_value={
                 "started_at": "2025-12-31T23:00:00+00:00",  # finished well before local check even started
                 "completed_at": "2025-12-31T23:05:00+00:00",
             }):
            rows = report.marker_overlap([self._comment(state="green")])
        self.assertEqual(rows[0]["overlap_seconds"], 0.0)
        self.assertEqual(rows[0]["post_ready_remaining_ci_seconds"], 0.0)

    def test_overlap_and_remaining_time_both_computed_when_ci_outruns_readiness(self):
        with mock.patch.object(report.gate_ci, "ci_conclusion", return_value=("green", "ok")), \
             mock.patch.object(report, "_raw_check_run", return_value={
                 "started_at": "2026-01-01T00:02:00+00:00",   # after local check started
                 "completed_at": "2026-01-01T00:10:00+00:00",  # after local check ended (00:05)
             }):
            rows = report.marker_overlap([self._comment(state="pending")])
        # overlap window: max(00:00,00:02)=00:02 .. min(00:05,00:10)=00:05 -> 180s
        self.assertEqual(rows[0]["overlap_seconds"], 180.0)
        # remaining: 00:10 - 00:05 -> 300s
        self.assertEqual(rows[0]["post_ready_remaining_ci_seconds"], 300.0)

    def test_re_query_is_gh_as_scoped(self):
        """Preclose finding 4: the two new read paths must not bypass the
        account-scoping every pre-existing read in this file already uses."""
        with mock.patch.object(report.gate_ci, "ci_conclusion", return_value=("green", "ok")) as m, \
             mock.patch.object(report, "_raw_check_run", return_value=None):
            report.marker_overlap([self._comment(state="pending")])
        self.assertEqual(m.call_args.kwargs.get("run"), report._gh_as_run)

    def test_a_still_pending_re_query_is_unresolved_not_dropped(self):
        """No wait, no retry -- if the re-query still says pending, that's
        the reported state. Preclose finding 3: this must remain visible
        as an explicit unresolved observation, not vanish from every count."""
        with mock.patch.object(report.gate_ci, "ci_conclusion", return_value=("pending", "still running")), \
             mock.patch.object(report, "_raw_check_run", return_value=None):
            rows = report.marker_overlap([self._comment(state="pending")])
        self.assertEqual(rows[0]["final_ci_state"], "pending")
        self.assertIsNone(rows[0]["post_ready_remaining_ci_seconds"])
        # Still present in the row list -- main()'s bucketing (not marker_overlap
        # itself) is what must count it as "unresolved", tested below.

    def test_duplicate_markers_for_the_same_pr_and_sha_dedup_to_one_row(self):
        """Preclose finding 5: a re-post, or any later comment quoting the
        marker verbatim, must not multiply one observation into several."""
        with mock.patch.object(report.gate_ci, "ci_conclusion", return_value=("green", "ok")), \
             mock.patch.object(report, "_raw_check_run", return_value=None):
            rows = report.marker_overlap([self._comment(state="green"), self._comment(state="green")])
        self.assertEqual(len(rows), 1)

    def test_different_shas_are_not_deduped_together(self):
        with mock.patch.object(report.gate_ci, "ci_conclusion", return_value=("green", "ok")), \
             mock.patch.object(report, "_raw_check_run", return_value=None):
            rows = report.marker_overlap([
                self._comment(state="green", head_sha="aaa111"),
                self._comment(state="green", head_sha="bbb222"),
            ])
        self.assertEqual(len(rows), 2)

    def test_a_malformed_ci_snapshot_at_does_not_raise(self):
        """Preclose finding 6: an unguarded fromisoformat() on a bad
        timestamp used to propagate out of marker_overlap() entirely."""
        comment = self._comment(state="green")
        comment["body"] = comment["body"].replace(
            "ci-snapshot-at=2026-01-01T00:05:00+00:00", "ci-snapshot-at=not-a-timestamp",
        )
        with mock.patch.object(report.gate_ci, "ci_conclusion", return_value=("green", "ok")), \
             mock.patch.object(report, "_raw_check_run", return_value={
                 "started_at": "2026-01-01T00:02:00+00:00", "completed_at": "2026-01-01T00:10:00+00:00",
             }):
            rows = report.marker_overlap([comment])  # must not raise
        self.assertEqual(len(rows), 1)

    def test_no_lane_pr_link_marker_yields_no_rows(self):
        self.assertEqual(report.marker_overlap([{"body": "plain discussion"}]), [])


def _row(overlap=None, remaining=None, snapshot_state="green"):
    return {
        "ci_snapshot_state": snapshot_state,
        "overlap_seconds": overlap,
        "post_ready_remaining_ci_seconds": remaining,
    }


class OverlapReportLinesTests(unittest.TestCase):
    """Preclose finding: main()'s bucketing/recommendation logic had zero
    test coverage of its own. Exercised directly here now that it's a pure
    function (`overlap_report_lines`), no network mock required."""

    def test_below_the_floor_reports_both_resolved_and_unresolved_counts(self):
        rows = [_row(0.0, 0.0) for _ in range(5)] + [_row(None, None) for _ in range(3)]
        lines = report.overlap_report_lines(rows)
        joined = "\n".join(lines)
        self.assertIn("resolved=5", joined)
        self.assertIn("unresolved=3", joined)
        self.assertIn("n=5 resolved (plus 3 unresolved", joined)
        self.assertIn("below the 20-observation bar", joined)

    def test_20_resolved_all_zero_overlap_recommends_retaining_the_flow(self):
        rows = [_row(0.0, 0.0) for _ in range(20)]
        lines = report.overlap_report_lines(rows)
        joined = "\n".join(lines)
        self.assertIn("retain the current flow", joined)

    def test_20_resolved_with_real_overlap_recommends_a_non_serializing_reduction(self):
        rows = [_row(120.0, 300.0) for _ in range(20)]
        lines = report.overlap_report_lines(rows)
        joined = "\n".join(lines)
        self.assertIn("non-serializing reduction", joined)
        self.assertIn("100% of readiness posts overlapped CI", joined)

    def test_a_mix_of_zero_and_nonzero_overlap_reaching_the_floor_computes_the_true_fraction(self):
        """This is finding 2's actual regression target: before the fix,
        zero-overlap rows never reached `resolved` at all, so a mix like
        this could never be represented."""
        rows = [_row(0.0, 0.0) for _ in range(15)] + [_row(120.0, 300.0) for _ in range(5)]
        lines = report.overlap_report_lines(rows)
        joined = "\n".join(lines)
        self.assertIn("resolved=20", joined)
        self.assertIn("25% of readiness posts overlapped CI", joined)

    def test_unknown_timing_rows_are_counted_separately_from_unresolved(self):
        rows = [_row(snapshot_state=None) for _ in range(4)] + [_row(None, None) for _ in range(2)]
        lines = report.overlap_report_lines(rows)
        joined = "\n".join(lines)
        self.assertIn("unknown-timing=4", joined)
        self.assertIn("unresolved=2", joined)


class ParseUtcTests(unittest.TestCase):
    def test_none_on_missing_value(self):
        self.assertIsNone(report._parse_utc(None))
        self.assertIsNone(report._parse_utc(""))

    def test_none_on_malformed_value(self):
        self.assertIsNone(report._parse_utc("not-a-timestamp"))

    def test_none_on_naive_value(self):
        """A timestamp with no offset can't be safely compared against the
        offset-aware ones this module produces -- treated as unusable
        rather than guessed at."""
        self.assertIsNone(report._parse_utc("2026-01-01T00:00:00"))

    def test_parses_a_well_formed_utc_value(self):
        parsed = report._parse_utc("2026-01-01T00:00:00+00:00")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.year, 2026)
