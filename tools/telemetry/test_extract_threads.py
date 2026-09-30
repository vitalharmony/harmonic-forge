#!/usr/bin/env python3
"""Tests for eras.py and extract_threads.py (harmonic-forge#829)."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import emit  # noqa: E402
import extract_threads as xt  # noqa: E402

FIXTURES = json.loads((HERE / "fixtures" / "threads.json").read_text())


def fake_get(repo_issues, calls):
    """REST stand-in serving the captured fixtures for any repo."""
    def get(path):
        calls.append(path)
        if "graphql" in path:
            raise AssertionError("GraphQL called")
        parts = path.split("?")[0].split("/")
        if parts[-1] == "issues":
            return [[{"number": int(n), "comments": 1} for n in repo_issues] + [{"number": 9, "comments": 0}]]
        number, kind = parts[-2], parts[-1]
        return [FIXTURES[number][kind]]
    return get


class ExtractTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Path(self._tmp.name)
        env = mock.patch.dict(os.environ, {emit.STORE_ENV: str(self.store)})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self._tmp.cleanup)
        self.calls: list[str] = []

    def _rows(self):
        return [json.loads(l) for p in sorted(self.store.rglob("*.jsonl")) for l in p.read_text().splitlines()]

    def _run(self, projects, issues=("1866", "1733")):
        identity = mock.Mock()
        quota = mock.Mock(side_effect=[100, 96] * len(projects))
        out = xt.run(projects, fake_get(issues, self.calls), identity, quota)
        return out, identity

    def _thread(self, issue):
        return sorted((r for r in self._rows() if r["issue"] == issue and r["source"] == "gh-thread"),
                      key=lambda r: r["ts"])

    def test_1866_yields_14_events_with_a_fail_then_pass_round(self):
        self._run([SimpleNamespace(name="h", repo="o/h", account="acct")], ("1866",))
        thread = self._thread(1866)
        self.assertEqual(len(thread), 14)
        gates = [r["event_type"] for r in thread if r["event_type"].startswith("gate.")]
        self.assertEqual(gates, ["gate.fail", "gate.pass"])
        self.assertTrue(all(r["provenance"] == "footer" for r in thread))

    def test_pre_0909_issue_yields_heading_provenance_l2_events(self):
        self._run([SimpleNamespace(name="h", repo="o/h", account="acct")], ("1733",))
        l2 = [r for r in self._thread(1733) if r["actor"] == "lane2"]
        self.assertTrue(l2)
        self.assertTrue(all(r["provenance"] == "heading" for r in l2))
        self.assertIn("gate.pass", [r["event_type"] for r in self._thread(1733)])

    def test_rerun_is_byte_identical(self):
        project = [SimpleNamespace(name="h", repo="o/h", account="acct")]
        self._run(project)
        first = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.store.rglob("*.jsonl")}
        out, _ = self._run(project)
        self.assertEqual(out[0]["written"], 0)
        self.assertEqual(first, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.store.rglob("*.jsonl")})

    def test_no_graphql_and_zero_comment_issues_skipped(self):
        self._run([SimpleNamespace(name="h", repo="o/h", account="acct")])
        self.assertFalse(any("graphql" in c for c in self.calls))
        self.assertFalse(any("/issues/9/" in c for c in self.calls))

    def test_quota_per_100_issues_recorded(self):
        out, _ = self._run([SimpleNamespace(name="h", repo="o/h", account="acct")])
        self.assertEqual(out[0]["quota_cost"], 4)
        self.assertEqual(out[0]["quota_per_100_issues"], 200.0)

    def test_second_account_with_zero_code_change(self):
        projects = [SimpleNamespace(name="a", repo="o/a", account="acct-one"),
                    SimpleNamespace(name="b", repo="other/b", account="acct-two")]
        _, identity = self._run(projects, ("1866",))
        self.assertEqual([c.args[0] for c in identity.call_args_list], ["o/a", "other/b"])
        self.assertTrue((self.store / "events/acct-one/o/gh-thread").is_dir())
        self.assertTrue((self.store / "events/acct-two/other/gh-thread").is_dir())
        self.assertEqual({r["repo"] for r in self._rows()}, {"o/a", "other/b"})

    def test_no_bodies_in_events(self):
        self._run([SimpleNamespace(name="h", repo="o/h", account="acct")])
        text = "".join(p.read_text() for p in self.store.rglob("*.jsonl"))
        self.assertNotIn('"body"', text)
        self.assertNotIn("Lane 3 Gate Results", text)

    def test_timeline_events_written(self):
        self._run([SimpleNamespace(name="h", repo="o/h", account="acct")], ("1866",))
        kinds = {r["event_type"] for r in self._rows() if r["source"] == "gh-timeline"}
        self.assertTrue({"issue.closed", "label.added", "cross-referenced"} <= kinds)

    def test_repo_less_entry_and_failing_repo_are_reported(self):
        projects = [SimpleNamespace(name="x", repo=None, account="a"),
                    SimpleNamespace(name="h", repo="o/h", account="acct")]
        with mock.patch.object(xt, "extract_repo", side_effect=FileNotFoundError("gh")):
            out = xt.run(projects, fake_get((), self.calls), mock.Mock(), mock.Mock(return_value=1))
        self.assertIn("no repo", out[0]["error"])
        self.assertIn("FileNotFoundError", out[1]["error"])


class RestGetTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        env = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": self._tmp.name, "GH_CONFIG_DIR": "slot"})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_graphql_refused(self):
        with self.assertRaises(xt.GraphQLRefused):
            xt.rest_get("graphql")

    def test_etag_304_serves_cache_and_follows_next_link(self):
        ok1 = 'HTTP/2.0 200 OK\r\nEtag: "a"\r\nLink: <https://api.github.com/r?page=2>; rel="next"\r\n\r\n[1]'
        ok2 = 'HTTP/2.0 200 OK\r\nEtag: "b"\r\n\r\n[2]'
        run = mock.Mock(side_effect=[SimpleNamespace(returncode=0, stdout=ok1, stderr=""),
                                     SimpleNamespace(returncode=0, stdout=ok2, stderr=""),
                                     SimpleNamespace(returncode=0, stdout="HTTP/2.0 304 Not Modified\r\n\r\n", stderr=""),
                                     SimpleNamespace(returncode=0, stdout="HTTP/2.0 304 Not Modified\r\n\r\n", stderr="")])
        with mock.patch.object(xt.subprocess, "run", run):
            self.assertEqual(xt.rest_get("r?page=1"), [[1], [2]])
            self.assertEqual(xt.rest_get("r?page=1"), [[1], [2]])
        self.assertIn("If-None-Match: \"a\"", run.call_args_list[2].args[0])

    def test_http_error_raises(self):
        run = mock.Mock(return_value=SimpleNamespace(returncode=1, stdout="HTTP/2.0 404 Not Found\r\n\r\n{}", stderr="nf"))
        with mock.patch.object(xt.subprocess, "run", run), self.assertRaises(RuntimeError):
            xt.rest_get("r")


if __name__ == "__main__":
    unittest.main()
