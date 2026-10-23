#!/usr/bin/env python3
"""Tests for pr_join_report.py and t2_pr_issue_join.sql (harmonic-forge#1167).

Fixture event files in a temp store, never the real one. DuckDB is not
installed in CI, so the view tests skip there with the install command.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import pr_join_report as pjr  # noqa: E402

try:
    import duckdb  # noqa: F401
    HAVE_DUCKDB = True
except ImportError:
    HAVE_DUCKDB = False
needs_duckdb = unittest.skipUnless(HAVE_DUCKDB, f"duckdb not installed ({pjr.INSTALL_HINT})")

REPO = "vitalharmony/widget"
RUN1, RUN2 = "2026-10-20T00:00:00Z", "2026-10-21T00:00:00Z"


def ev(event_type: str, ts: str, *, repo: str = REPO, account: str = "vitalharmony", pr=None, issue=None,
       provenance=None, **attrs) -> dict:
    if event_type == "fetch.status":  # the fetcher carries a status's PR in attrs.pr
        attrs["pr"], pr = pr, None
    return {"event_type": event_type, "source": "gh-pr", "provenance": provenance, "account": account,
            "org": repo.split("/")[0], "repo": repo, "pr": pr, "issue": issue, "ts": ts,
            "subject_id": f"{repo}#{pr or issue}", "attrs": attrs}


def merged(pr: int, at: str, **kw) -> list[dict]:
    """A merged PR fully observed in RUN1."""
    return [ev("pr.merged", at, pr=pr, **kw), ev("fetch.status", RUN1, status="ok", stage="pr", pr=pr, **kw)]


def subject(pr: int, issue: int, at: str, target: str = REPO, **kw) -> dict:
    return ev("pr.link.observed", at, pr=pr, provenance="git-subject", link_kind="subject",
              target_repo=target, target_issue=issue, **kw)


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Path(self._tmp.name) / "store"

    def write(self, events: list[dict], name: str = "a.jsonl") -> None:
        path = self.store / "events" / "x" / "y" / "gh-pr" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event) + "\n")

    def derive(self, **filters) -> dict:
        rows, _ = pjr.read_events(self.store, **filters)
        return pjr.derive(rows)

    def states(self, **filters) -> dict[int, tuple]:
        _, rows = self.derive(**filters)["pr_issue_join"]
        return {r[3]: (r[6], r[5]) for r in rows}  # pr -> (state, issue)

    def coverage(self) -> list[tuple]:
        return self.derive()["pr_join_coverage"][1]


@needs_duckdb
class JoinTests(Fixture):
    def test_subject_joins_and_no_link_is_unjoined(self) -> None:
        self.write(merged(1, "2026-10-01T00:00:00Z") + [subject(1, 7, "2026-10-01T00:00:00Z")]
                   + merged(2, "2026-10-02T00:00:00Z"))
        self.assertEqual(self.states(), {1: ("joined", 7), 2: ("unjoined", None)})

    def test_connected_is_unresolved(self) -> None:
        self.write(merged(3, "2026-10-01T00:00:00Z") + [ev("pr.link.observed", "2026-10-01T00:00:00Z", pr=3,
                   provenance="timeline", link_kind="connected", target_repo=None, target_issue=None)])
        self.assertEqual(self.states()[3][0], "unresolved")

    def test_no_per_pr_status_is_unresolved(self) -> None:
        self.write([ev("pr.merged", "2026-10-01T00:00:00Z", pr=4)])
        self.assertEqual(self.states()[4][0], "unresolved")

    def test_a_later_ok_run_clears_a_failure(self) -> None:
        self.write([ev("pr.merged", "2026-10-01T00:00:00Z", pr=5),
                    ev("fetch.status", RUN1, status="failed", stage="timeline", pr=5)])
        self.assertEqual(self.states()[5][0], "unresolved")
        self.write([ev("fetch.status", RUN2, status="ok", stage="pr", pr=5)])
        self.assertEqual(self.states()[5][0], "unjoined")

    def test_a_failure_in_the_newest_run_wins_over_an_older_ok(self) -> None:
        self.write(merged(6, "2026-10-01T00:00:00Z")
                   + [ev("fetch.status", RUN2, status="failed", stage="emit", pr=6)])
        self.assertEqual(self.states()[6][0], "unresolved")

    def test_cross_reference_joins_only_within_24_hours_of_close(self) -> None:
        xref = lambda pr, issue: ev("pr.link.observed", "2026-10-01T00:00:00Z", pr=pr, provenance="timeline",  # noqa: E731
                                    link_kind="cross-referenced", target_repo=REPO, target_issue=issue)
        self.write(merged(8, "2026-10-01T10:00:00Z") + [xref(8, 20), ev("issue.closed", "2026-10-02T09:00:00Z", issue=20)]
                   + merged(9, "2026-10-01T10:00:00Z") + [xref(9, 21), ev("issue.closed", "2026-10-03T11:00:00Z", issue=21)])
        self.assertEqual(self.states()[8], ("joined", 20))
        self.assertEqual(self.states()[9][0], "unjoined")

    def test_a_late_close_turns_unjoined_into_joined_with_no_store_edit(self) -> None:
        self.write(merged(10, "2026-10-01T10:00:00Z") + [ev("pr.link.observed", "2026-10-01T00:00:00Z", pr=10,
                   provenance="timeline", link_kind="cross-referenced", target_repo=REPO, target_issue=30)])
        self.assertEqual(self.states()[10][0], "unjoined")
        self.write([ev("issue.closed", "2026-10-01T12:00:00Z", issue=30)], name="b.jsonl")
        self.assertEqual(self.states()[10], ("joined", 30))

    def test_one_pr_in_two_partitions_is_one_pr(self) -> None:
        self.write(merged(13, "2026-10-01T00:00:00Z") + [ev("pr.merged", "2026-10-01T00:00:00Z", pr=13, account="unresolved")]
                   + [subject(13, 7, "2026-10-01T00:00:00Z")] + merged(14, "2026-10-01T00:00:00Z"))
        self.assertEqual(self.states(), {13: ("joined", 7), 14: ("unjoined", None)})
        self.assertEqual(self.coverage()[0][2:5], (50.0, 1, 1))

    def test_a_reclosure_within_24_hours_joins(self) -> None:
        self.write(merged(15, "2026-10-05T10:00:00Z") + [ev("pr.link.observed", "2026-10-05T00:00:00Z", pr=15,
                   provenance="timeline", link_kind="cross-referenced", target_repo=REPO, target_issue=40),
                   ev("issue.closed", "2026-09-01T00:00:00Z", issue=40), ev("issue.closed", "2026-10-05T12:00:00Z", issue=40)])
        self.assertEqual(self.states()[15], ("joined", 40))

    def test_a_cross_repo_target_never_joins(self) -> None:
        self.write(merged(11, "2026-10-01T00:00:00Z") + [subject(11, 7, "2026-10-01T00:00:00Z", target="other/repo")])
        self.assertEqual(self.states()[11][0], "unjoined")

    def test_same_repo_compares_case_insensitively(self) -> None:
        self.write(merged(12, "2026-10-01T00:00:00Z") + [subject(12, 7, "2026-10-01T00:00:00Z", target="VitalHarmony/Widget")])
        self.assertEqual(self.states()[12], ("joined", 7))


@needs_duckdb
class CoverageTests(Fixture):
    def test_unresolved_is_excluded_from_the_denominator(self) -> None:
        self.write(merged(1, "2026-10-01T00:00:00Z") + [subject(1, 7, "2026-10-01T00:00:00Z")]
                   + merged(2, "2026-10-02T00:00:00Z") + [ev("pr.merged", "2026-10-03T00:00:00Z", pr=3)])
        row = self.coverage()[0]
        self.assertEqual(row[:6], (REPO, "2026-10", 50.0, 1, 1, 1))

    def test_a_capped_run_caveats_months_up_to_the_oldest_kept(self) -> None:
        self.write(merged(1, "2026-09-15T00:00:00Z") + merged(2, "2026-10-15T00:00:00Z") + merged(3, "2026-11-15T00:00:00Z")
                   + [ev("fetch.status", RUN1, status="capped", stage="list", oldest_merged_at="2026-10-15T00:00:00Z")])
        caveats = {r[1]: r[6] for r in self.coverage()}
        self.assertEqual(caveats, {"2026-09": "capped", "2026-10": "capped", "2026-11": ""})

    def test_a_per_pr_failure_caveats_its_month(self) -> None:
        self.write([ev("pr.merged", "2026-10-01T00:00:00Z", pr=1),
                    ev("fetch.status", RUN1, status="failed", stage="timeline", pr=1)] + merged(2, "2026-11-01T00:00:00Z"))
        self.assertEqual({r[1]: r[6] for r in self.coverage()}, {"2026-10": "fetch failed", "2026-11": ""})

    def test_a_capped_run_that_kept_nothing_caveats_every_month(self) -> None:
        self.write(merged(1, "2026-09-15T00:00:00Z") + merged(2, "2026-10-15T00:00:00Z")
                   + [ev("fetch.status", RUN1, status="capped", stage="list", oldest_merged_at=None)])
        self.assertEqual({r[1]: r[6] for r in self.coverage()}, {"2026-09": "capped", "2026-10": "capped"})

    def test_a_later_uncapped_ok_run_clears_the_capped_caveat(self) -> None:
        self.write(merged(1, "2026-09-15T00:00:00Z")
                   + [ev("fetch.status", RUN1, status="capped", stage="list", oldest_merged_at="2026-10-15T00:00:00Z"),
                      ev("fetch.status", RUN1, status="ok", stage="repo")])
        self.assertEqual(self.coverage()[0][6], "capped")
        self.write([ev("fetch.status", RUN2, status="ok", stage="repo")], name="b.jsonl")
        self.assertEqual(self.coverage()[0][6], "")

    def test_a_status_only_failed_repo_still_shows(self) -> None:
        self.write([ev("fetch.status", RUN1, status="failed", stage="list")])
        self.assertEqual([r[:7] for r in self.coverage()], [(REPO, "2026-10", None, 0, 0, 0, "fetch failed")])

    def test_a_status_only_capped_run_that_kept_nothing_still_shows(self) -> None:
        self.write([ev("fetch.status", RUN1, status="capped", stage="list", oldest_merged_at=None)])
        self.assertEqual([r[:7] for r in self.coverage()], [(REPO, "2026-10", None, 0, 0, 0, "capped")])

    def test_a_cleared_failure_leaves_no_status_only_row(self) -> None:
        self.write([ev("fetch.status", RUN1, status="failed", stage="list"),
                    ev("fetch.status", RUN2, status="ok", stage="repo")])
        self.assertEqual(self.coverage(), [])

    def test_a_status_only_second_account_repo_shows_under_its_own_repo(self) -> None:
        other = "secondorg/gadget"
        self.write([ev("fetch.status", RUN1, status="failed", stage="list", repo=other, account="secondaccount")]
                   + merged(1, "2026-10-01T00:00:00Z"))
        self.assertEqual({(r[0], r[6]) for r in self.coverage()}, {(other, "fetch failed"), (REPO, "")})

    def test_a_repo_failure_with_no_later_ok_caveats_every_month(self) -> None:
        self.write(merged(1, "2026-10-01T00:00:00Z") + [ev("fetch.status", RUN2, status="failed", stage="list")])
        self.assertEqual(self.coverage()[0][6], "fetch failed")
        self.write([ev("fetch.status", "2026-10-22T00:00:00Z", status="ok", stage="repo")], name="b.jsonl")
        self.assertEqual(self.coverage()[0][6], "")


@needs_duckdb
class MedianTests(Fixture):
    def pairs(self, n: int, *, repo: str = REPO) -> list[dict]:
        events = []
        for i in range(n):
            events += merged(100 + i, "2026-10-02T00:00:00Z", repo=repo)
            events += [subject(100 + i, 500 + i, "2026-10-02T00:00:00Z", target=repo, repo=repo),
                       ev("issue.opened", "2026-10-01T00:00:00Z", issue=500 + i, repo=repo)]
        return events

    def medians(self) -> dict[str, tuple]:
        return {r[0]: (r[1], r[2]) for r in self.derive()["pr_join_medians"][1]}

    def test_reported_only_at_20_pairs(self) -> None:
        self.write(self.pairs(19))
        self.assertEqual(self.medians()["all"], (None, 19))
        self.write(self.pairs(20), name="b.jsonl")
        self.assertEqual(self.medians()["all"], (24.0, 20))

    def test_a_pr_joined_to_two_issues_pairs_both(self) -> None:
        events = self.pairs(19)
        events += merged(800, "2026-10-02T00:00:00Z") + [
            subject(800, 700, "2026-10-02T00:00:00Z"),
            ev("pr.link.observed", "2026-10-02T00:00:00Z", pr=800, provenance="pr-link-marker", link_kind="marker",
               target_repo=REPO, target_issue=701),
            ev("issue.opened", "2026-10-01T00:00:00Z", issue=700), ev("issue.opened", "2026-10-01T00:00:00Z", issue=701)]
        self.write(events)
        self.assertEqual(self.medians()["all"], (24.0, 21))

    def test_earliest_merge_per_issue(self) -> None:
        events = self.pairs(20)
        # Rework PRs on 11 of the 20 issues, merged a week later, must not move
        # the pairs: keeping the latest merge would move the median to 192h.
        for i in range(11):
            events += merged(900 + i, "2026-10-09T00:00:00Z") + [subject(900 + i, 500 + i, "2026-10-09T00:00:00Z")]
        self.write(events)
        self.assertEqual(self.medians()["all"], (24.0, 20))


@needs_duckdb
class PartitionTests(Fixture):
    def test_second_account_with_zero_code_change(self) -> None:
        other = "secondorg/gadget"
        self.write(merged(1, "2026-10-01T00:00:00Z")
                   + merged(2, "2026-10-01T00:00:00Z", repo=other, account="secondaccount"))
        self.assertEqual(sorted(r[0] for r in self.coverage()), [other, REPO])
        _, rows = self.derive(account="secondaccount")["pr_issue_join"]
        self.assertEqual({r[0] for r in rows}, {other})


class PureTests(Fixture):
    def test_malformed_line_is_counted_and_skipped(self) -> None:
        self.write(merged(1, "2026-10-01T00:00:00Z"))
        path = next(self.store.rglob("*.jsonl"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"torn\n[1]\n')
        rows, malformed = pjr.read_events(self.store)
        self.assertEqual((len(rows), malformed), (2, 2))

    def test_empty_store_reads_empty(self) -> None:
        self.assertEqual(pjr.read_events(self.store / "absent"), ([], 0))


if __name__ == "__main__":
    unittest.main()
