#!/usr/bin/env python3
"""Tests for fetch_pr_observations.py (harmonic-forge#1166, F637 T2a).

`gh` is faked by an injected callable and the store is a temp directory, so
no test calls GitHub or touches the real telemetry store.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import emit  # noqa: E402
import fetch_pr_observations as fpo  # noqa: E402

STARTED = "2026-10-23T00:00:00Z"
def marker(issue_repo: str, issue: int, pr_repo: str, pr: int) -> str:
    return (f"<!-- lane-pr-link v1; issue-repo={issue_repo}; issue={issue}; issue-node-id=I_x; "
            f"pr-repo={pr_repo}; pr={pr}; pr-node-id=PR_x; head-sha=abc; -->")


def _manifest(tmp: Path, entries: list[tuple[str, str, str, str]]) -> Path:
    body = ""
    for name, prefix, repo, account in entries:
        body += (f'[[project]]\nname = "{name}"\nprefix = "{prefix}"\n'
                 f'repo = "{repo}"\naccount = "{account}"\npath = "{tmp / name}"\n\n')
    target = tmp / "projects.toml"
    target.write_text(body, encoding="utf-8")
    return target


class FakeGh:
    """Longest-prefix match on the API path; a BaseException value is raised."""

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, path: str):
        self.calls.append(path)
        for prefix in sorted(self.routes, key=len, reverse=True):
            if path.startswith(prefix):
                value = self.routes[prefix]
                if isinstance(value, BaseException):
                    raise value
                return json.loads(json.dumps(value))
        raise fpo.FetchError(f"no route for {path}")


def _routes(repo: str = "vitalharmony/widget", *, timeline=None) -> dict[str, object]:
    return {
        f"repos/{repo}/pulls?": [
            {"number": 11, "title": "Fix the thing (widget#7) (#11)", "merged_at": "2026-10-01T10:00:00Z",
             "merge_commit_sha": "sha11"},
            {"number": 12, "title": "No link here", "merged_at": "2026-10-02T10:00:00Z",
             "merge_commit_sha": "sha12"},
            {"number": 13, "title": "Closed unmerged", "merged_at": None},
        ],
        f"repos/{repo}/issues/comments?": [
            {"id": 501, "created_at": "2026-10-01T09:00:00Z", "body": marker(repo, 7, repo, 11)},
            {"id": 502, "created_at": "2026-10-01T09:30:00Z", "body": "unrelated"},
        ],
        f"repos/{repo}/issues/11/timeline": [
            {"event": "cross-referenced", "created_at": "2026-10-01T08:00:00Z",
             "source": {"issue": {"number": 7, "repository": {"full_name": repo}}}},
        ],
        f"repos/{repo}/issues/12/timeline": timeline if timeline is not None else [
            {"event": "connected", "created_at": "2026-10-02T09:00:00Z"},
        ],
        f"repos/{repo}/issues/7": {"created_at": "2026-09-30T10:00:00Z", "closed_at": "2026-10-01T10:05:00Z"},
        f"repos/{repo}/commits/sha11/check-runs": [
            {"total_count": 1, "check_runs": [{"id": 9001, "name": "verify", "status": "completed",
                                               "conclusion": "success", "completed_at": "2026-10-01T10:10:00Z"}]}],
        f"repos/{repo}/commits/sha12/check-runs": [{"total_count": 0, "check_runs": []}],
    }


class FetchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.store = self.tmp / "store"
        env = mock.patch.dict(os.environ, {emit.STORE_ENV: str(self.store)})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self._tmp.cleanup)
        self.manifest = _manifest(self.tmp, [("widget", "W", "vitalharmony/widget", "vitalharmony"),
                                             ("gadget", "G", "secondorg/gadget", "secondaccount")])

    def run_repo(self, repo: str, gh, *, limit=None, identity=lambda r: None, started=STARTED):
        return fpo.fetch_repo(repo, gh, limit=limit, started=started,
                              registry=["secondorg/gadget", "vitalharmony/widget"],
                              manifest_file=self.manifest, identity=identity)

    def events(self) -> list[dict]:
        rows = []
        for path in sorted(self.store.rglob("*.jsonl")):
            rows += [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return rows

    def of(self, event_type: str) -> list[dict]:
        return [e for e in self.events() if e["event_type"] == event_type]

    def statuses(self) -> list[dict]:
        return [e["attrs"] for e in self.of("fetch.status")]

    def test_observations_carry_no_join_decision(self) -> None:
        self.run_repo("vitalharmony/widget", FakeGh(_routes()))
        merged = self.of("pr.merged")
        self.assertEqual(sorted(e["pr"] for e in merged), [11, 12])
        links = self.of("pr.link.observed")
        self.assertEqual(sorted(e["attrs"]["link_kind"] for e in links),
                         ["connected", "cross-referenced", "marker", "subject"])
        for event in merged + links:
            self.assertIsNone(event.get("issue"), event["subject_id"])
        subject = next(e for e in links if e["attrs"]["link_kind"] == "subject")
        self.assertEqual((subject["attrs"]["target_repo"], subject["attrs"]["target_issue"]), ("vitalharmony/widget", 7))
        self.assertEqual([e["issue"] for e in self.of("issue.opened")], [7])
        self.assertEqual([e["issue"] for e in self.of("issue.closed")], [7])
        self.assertEqual([e["attrs"]["conclusion"] for e in self.of("ci.conclusion")], ["success"])

    def test_rerun_writes_no_new_observation_events(self) -> None:
        self.run_repo("vitalharmony/widget", FakeGh(_routes()))
        first = [e for e in self.events() if e["event_type"] != "fetch.status"]
        self.run_repo("vitalharmony/widget", FakeGh(_routes()), started="2026-10-24T00:00:00Z")
        second = [e for e in self.events() if e["event_type"] != "fetch.status"]
        self.assertEqual(len(first), len(second))

    def test_a_new_link_on_rerun_adds_one_event_and_rewrites_none(self) -> None:
        self.run_repo("vitalharmony/widget", FakeGh(_routes()))
        ids = {e["event_id"] for e in self.events() if e["event_type"] != "fetch.status"}
        later = [{"event": "connected", "created_at": "2026-10-02T09:00:00Z"},
                 {"event": "cross-referenced", "created_at": "2026-10-03T08:00:00Z",
                  "source": {"issue": {"number": 9, "repository": {"full_name": "vitalharmony/widget"}}}}]
        routes = {**_routes(timeline=later), "repos/vitalharmony/widget/issues/9": {"created_at": "2026-10-03T07:00:00Z"}}
        self.run_repo("vitalharmony/widget", FakeGh(routes), started="2026-10-24T00:00:00Z")
        new = {e["event_id"] for e in self.events() if e["event_type"] != "fetch.status"} - ids
        kinds = sorted(e["event_type"] for e in self.events() if e["event_id"] in new)
        self.assertEqual(kinds, ["issue.opened", "pr.link.observed"])

    def test_connected_is_recorded_with_a_null_target(self) -> None:
        self.run_repo("vitalharmony/widget", FakeGh(_routes()))
        connected = [e for e in self.of("pr.link.observed") if e["attrs"]["link_kind"] == "connected"]
        self.assertEqual(len(connected), 1)
        self.assertIsNone(connected[0]["attrs"]["target_issue"])

    def test_a_timeline_failure_is_recorded_and_the_next_pr_still_runs(self) -> None:
        routes = _routes()
        routes["repos/vitalharmony/widget/issues/11/timeline"] = fpo.FetchError("HTTP 502")
        self.run_repo("vitalharmony/widget", FakeGh(routes))
        failed = [s for s in self.statuses() if s["status"] == "failed"]
        self.assertEqual([(s["stage"], s["pr"]) for s in failed], [("timeline", 11)])
        ok_prs = [s["pr"] for s in self.statuses() if s["status"] == "ok" and s["stage"] == "pr"]
        self.assertEqual(ok_prs, [12])
        self.assertIn(12, [e["pr"] for e in self.of("pr.merged")])

    def test_a_crash_mid_pr_leaves_no_per_pr_ok(self) -> None:
        routes = _routes()
        routes["repos/vitalharmony/widget/commits/sha11/check-runs"] = RuntimeError("boom")
        self.run_repo("vitalharmony/widget", FakeGh(routes))
        ok_prs = [s["pr"] for s in self.statuses() if s["status"] == "ok" and s["stage"] == "pr"]
        self.assertNotIn(11, ok_prs)
        self.assertIn(("ci", 11), [(s["stage"], s["pr"]) for s in self.statuses() if s["status"] == "failed"])

    def test_identity_exit_is_recorded_and_the_next_repo_still_runs(self) -> None:
        def identity(repo):
            if repo == "vitalharmony/widget":
                raise SystemExit("identity mismatch")
        self.run_repo("vitalharmony/widget", FakeGh(_routes()), identity=identity)
        self.run_repo("secondorg/gadget", FakeGh(_routes("secondorg/gadget")), identity=identity)
        failed = [(e["repo"], e["attrs"]["stage"]) for e in self.of("fetch.status") if e["attrs"]["status"] == "failed"]
        self.assertEqual(failed, [("vitalharmony/widget", "identity")])
        self.assertTrue(any(e["repo"] == "secondorg/gadget" for e in self.of("pr.merged")))

    def test_a_list_failure_is_recorded(self) -> None:
        routes = _routes()
        routes["repos/vitalharmony/widget/pulls?"] = fpo.FetchError("HTTP 403")
        self.run_repo("vitalharmony/widget", FakeGh(routes))
        self.assertEqual([(s["status"], s["stage"]) for s in self.statuses()], [("failed", "list")])

    def test_a_capped_repo_records_the_cap(self) -> None:
        self.run_repo("vitalharmony/widget", FakeGh(_routes()), limit=1)
        capped = [s for s in self.statuses() if s["status"] == "capped"]
        self.assertEqual(len(capped), 1)
        self.assertEqual((capped[0]["merged_seen"], capped[0]["oldest_merged_at"]), (1, "2026-10-02T10:00:00Z"))
        self.assertEqual([e["pr"] for e in self.of("pr.merged")], [12])

    def test_a_marker_naming_another_repos_pr_is_recorded_for_that_pr(self) -> None:
        routes = _routes()
        routes["repos/vitalharmony/widget/issues/comments?"].append(
            {"id": 777, "created_at": "2026-10-04T09:00:00Z",
             "body": marker("vitalharmony/widget", 8, "secondorg/gadget", 42)})
        self.run_repo("vitalharmony/widget", FakeGh(routes))
        cross = [e for e in self.of("pr.link.observed") if e["subject_id"].startswith("secondorg/gadget#42:")]
        self.assertEqual(len(cross), 1)
        self.assertEqual((cross[0]["account"], cross[0]["repo"]), ("secondaccount", "secondorg/gadget"))
        self.assertEqual((cross[0]["attrs"]["target_repo"], cross[0]["attrs"]["target_issue"]),
                         ("vitalharmony/widget", 8))
        self.assertIsNone(cross[0].get("issue"))

    def test_an_emit_rejection_fails_the_pr_instead_of_ok(self) -> None:
        routes = _routes()
        routes["repos/vitalharmony/widget/commits/sha11/check-runs"] = [
            {"total_count": 1, "check_runs": [{"id": 9002, "name": "x" * 300, "status": "completed",
                                               "conclusion": "success", "completed_at": "2026-10-01T10:10:00Z"}]}]
        self.run_repo("vitalharmony/widget", FakeGh(routes))
        ok_prs = [s["pr"] for s in self.statuses() if s["status"] == "ok" and s["stage"] == "pr"]
        self.assertNotIn(11, ok_prs)
        self.assertIn(("emit", 11), [(s["stage"], s["pr"]) for s in self.statuses() if s["status"] == "failed"])

    def test_limit_zero_records_the_cap_without_crashing(self) -> None:
        self.run_repo("vitalharmony/widget", FakeGh(_routes()), limit=0)
        self.assertEqual([(s["status"], s["oldest_merged_at"]) for s in self.statuses() if s["status"] == "capped"],
                         [("capped", None)])
        self.assertIn(("ok", "repo"), [(s["status"], s["stage"]) for s in self.statuses()])

    def test_a_malformed_marker_fails_only_its_comment(self) -> None:
        routes = _routes()
        routes["repos/vitalharmony/widget/issues/comments?"].append(
            {"id": 888, "created_at": "2026-10-04T09:00:00Z", "body": marker("vitalharmony/widget", 8, "not a repo", 5)})
        self.run_repo("vitalharmony/widget", FakeGh(routes))
        self.assertEqual(sorted(e["pr"] for e in self.of("pr.merged")), [11, 12])
        self.assertIn(("failed", "marker", 888),
                      [(s["status"], s["stage"], s.get("comment_id")) for s in self.statuses()])
        self.assertNotIn("list", [s["stage"] for s in self.statuses() if s["status"] == "failed"])

    def test_a_malformed_issue_repo_on_a_cross_repo_marker_is_recorded_not_raised(self) -> None:
        routes = _routes()
        routes["repos/vitalharmony/widget/issues/comments?"].append(
            {"id": 889, "created_at": "2026-10-04T09:00:00Z", "body": marker("bad repo!", 8, "secondorg/gadget", 42)})
        self.run_repo("vitalharmony/widget", FakeGh(routes))
        cross = [e for e in self.of("pr.link.observed") if e["subject_id"].startswith("secondorg/gadget#42:")]
        self.assertEqual(len(cross), 1)
        self.assertIsNone(cross[0]["attrs"]["target_repo"])
        self.assertNotIn("target_ref", cross[0]["attrs"])
        self.assertNotIn("bad repo!", json.dumps(cross[0]))
        self.assertEqual(sorted(e["pr"] for e in self.of("pr.merged") if e["repo"] == "vitalharmony/widget"), [11, 12])

    def test_two_cross_repo_failures_in_one_run_are_two_statuses(self) -> None:
        routes = _routes()
        for cid, pr in ((901, 42), (902, 43)):
            routes["repos/vitalharmony/widget/issues/comments?"].append(
                {"id": cid, "created_at": "2026-10-04T09:00:00Z", "body": marker("vitalharmony/widget", 8, "secondorg/gadget", pr)})
        real = fpo.record_marker

        def failing(rec, pr_repo, number, comment):
            if pr_repo == "secondorg/gadget":
                raise RuntimeError("boom")
            return real(rec, pr_repo, number, comment)
        with mock.patch.object(fpo, "record_marker", failing):
            self.run_repo("vitalharmony/widget", FakeGh(routes))
        failed = sorted(s["marker_pr"] for s in self.statuses() if s["status"] == "failed" and s["stage"] == "marker")
        self.assertEqual(failed, ["secondorg/gadget#42", "secondorg/gadget#43"])

    def test_a_non_ascii_digit_marker_field_is_null_not_a_crash(self) -> None:
        routes = _routes()
        routes["repos/vitalharmony/widget/issues/comments?"].append(
            {"id": 890, "created_at": "2026-10-04T09:00:00Z",
             "body": marker("vitalharmony/widget", 7, "vitalharmony/widget", 11).replace("issue=7;", "issue=\u00b2;")})
        self.run_repo("vitalharmony/widget", FakeGh(routes))
        ok_prs = [s["pr"] for s in self.statuses() if s["status"] == "ok" and s["stage"] == "pr"]
        self.assertIn(11, ok_prs)
        odd = [e for e in self.of("pr.link.observed") if e["subject_id"].endswith(":pr-link-marker:890")]
        self.assertEqual(len(odd), 1)
        self.assertIsNone(odd[0]["attrs"]["target_issue"])

    def test_second_account_with_zero_code_change(self) -> None:
        self.run_repo("secondorg/gadget", FakeGh(_routes("secondorg/gadget")))
        accounts = {(e["account"], e["org"]) for e in self.events()}
        self.assertEqual(accounts, {("secondaccount", "secondorg")})

    def test_an_unregistered_repo_is_attributed_unresolved(self) -> None:
        self.assertEqual(fpo.attribute("strangers/thing", self.manifest)["account"], "unresolved")

    def test_subject_slug_resolution(self) -> None:
        registry = ["secondorg/gadget", "vitalharmony/widget", "vitalharmony/hrse"]
        self.assertEqual(fpo.target_repo_for("hrse", "vitalharmony/widget", registry), "vitalharmony/hrse")
        self.assertEqual(fpo.target_repo_for("Other/Repo", "vitalharmony/widget", registry), "other/repo")
        self.assertIsNone(fpo.target_repo_for("nowhere", "vitalharmony/widget", registry))


if __name__ == "__main__":
    unittest.main()
