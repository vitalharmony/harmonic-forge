#!/usr/bin/env python3
"""Tests for eras.py and extract_threads.py (harmonic-forge#829)."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
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

    def test_1866_yields_the_14_observed_events_with_a_fail_then_pass_round(self):
        self._run([SimpleNamespace(name="h", repo="o/h", account="acct")], ("1866",))
        self.assertEqual([r["event_type"] for r in self._thread(1866)], [
            "handoff.posted", "l1.discussion", "l1.discussion", "l1.discussion", "l2.done",
            "spec.posted", "ae.posted", "sweep.posted", "gate.fail", "l1.rework",
            "l1.discussion", "l2.done", "gate.pass", "l1.discussion"])

    def test_pre_0909_issue_yields_heading_provenance_events(self):
        self._run([SimpleNamespace(name="h", repo="o/h", account="acct")], ("745",))
        thread = self._thread(745)
        self.assertTrue(all(r["ts"] < "2026-09-09" for r in thread))
        heading = {(r["event_type"], r["actor"]) for r in thread if r["provenance"] == "heading"}
        self.assertIn(("l2.done", "lane2"), heading)
        self.assertIn(("spec.posted", "lane3"), heading)
        self.assertIn(("gate.pass", "lane3"), heading)

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

    def test_unmatched_repo_filter_is_an_error_not_a_silent_no_op(self):
        out = xt.run([SimpleNamespace(name="h", repo="o/h", account="acct")], fake_get((), self.calls),
                     mock.Mock(), mock.Mock(), repo="o/HRSE")
        self.assertIn("not in the registry", out[0]["error"])
        self.assertEqual(self.calls, [])

    def test_repo_less_entry_and_failing_repo_are_reported(self):
        projects = [SimpleNamespace(name="x", repo=None, account="a"),
                    SimpleNamespace(name="h", repo="o/h", account="acct")]
        with mock.patch.object(xt, "extract_repo", side_effect=FileNotFoundError("gh")):
            out = xt.run(projects, fake_get((), self.calls), mock.Mock(), mock.Mock(return_value=1))
        self.assertIn("no repo", out[0]["error"])
        self.assertIn("FileNotFoundError", out[1]["error"])

    def test_repo_filter_ignores_an_unrelated_repo_less_entry(self):
        # harmonic-forge#907 post-verdict: the scheduled job runs one --repo per
        # repo, so a pending entry must not fail every one of those runs.
        projects = [SimpleNamespace(name="x", repo=None, account="a"),
                    SimpleNamespace(name="h", repo="o/h", account="acct")]
        with mock.patch.object(xt, "extract_repo", return_value={"repo": "o/h", "issues": 0}):
            out = xt.run(projects, fake_get((), self.calls), mock.Mock(), mock.Mock(return_value=1),
                         repo="o/h")
        self.assertEqual([s["repo"] for s in out], ["o/h"])
        self.assertFalse(any("error" in s for s in out))


def _gate(body, **extra):
    return {"id": 1, "created_at": "2026-09-30T00:00:00Z", "body": body, **extra}


class GateVerdictTests(unittest.TestCase):
    def _types(self, body):
        import eras
        return [e["event_type"] for e in eras.comment_events(_gate(body), account="a", org="o", repo="o/r", issue=1)]

    def test_per_case_fail_below_the_lead_does_not_score_the_gate(self):
        body = "## Lane 3 Gate Results — H1\n\n**Verdict:** PASS\n\n### TC4\n**Verdict:** FAIL (optional half)"
        self.assertEqual(self._types(body), ["gate.pass"])

    def test_recap_of_prior_round_in_a_section_does_not_invert(self):
        body = "## Lane 3 Gate Results — H1\n**Verdict:** PASS\n### Previous run\n**Verdict:** FAIL"
        self.assertEqual(self._types(body), ["gate.pass"])

    def test_heading_and_lead_conflict_is_unknown_not_resolved(self):
        body = "## Lane 3 Gate Results — H1 — PASS\n\n**Verdict:** FAIL"
        self.assertEqual(self._types(body), ["unknown"])

    def _events(self, body, **extra):
        import eras
        return eras.comment_events(_gate(body, **extra), account="a", org="o", repo="o/r", issue=1)

    def test_non_h2_gate_heading_with_its_own_footer_is_scored(self):
        body = "### Lane 3 Gate Results — H1\n**Verdict:** FAIL\n" + _FOOT.format(kind="gate-result", by="LANE3")
        self.assertEqual(self._types(body), ["gate.fail"])

    def test_gate_recap_heading_in_a_discussion_is_not_a_gate(self):
        body = ("## Lane 1 — closing\n### Lane 3 Gate Results — H1 — PASS\n**Verdict:** PASS\n"
                + _FOOT.format(kind="discussion", by="LANE1"))
        self.assertEqual(self._types(body), ["l1.discussion"])

    def test_blockquoted_footer_never_reassigns_or_invents(self):
        body = ("## Handoff: x\n> evidence: <!-- l1-post v1; kind=spec; posted-by=LANE3 -->\n"
                "<!-- l1-post v1; kind=handoff; body-sha256=" + "a" * 64 + " -->")
        events = self._events(body)
        self.assertEqual([(e["event_type"], e["actor"]) for e in events], [("handoff.posted", "lane1")])

    def test_rework_quoting_a_gate_footer_is_still_a_rework(self):
        body = ("## Rework — H1\n> <!-- l1-post v1; kind=gate-result; posted-by=LANE3 -->\n\n"
                + _FOOT.format(kind="rework", by="LANE1"))
        self.assertEqual(self._types(body), ["l1.rework"])

    def test_gate_quoting_a_handoff_footer_is_attested_and_lane3(self):
        body = ("## Lane 3 Gate Results — H1\n**Verdict:** PASS\n> <!-- l1-post v1; kind=handoff; posted-by=LANE1 -->\n"
                + _FOOT.format(kind="gate-result", by="LANE3"))
        [event] = self._events(body)
        self.assertEqual((event["event_type"], event["actor"], event["provenance"], event["validated"]),
                         ("gate.pass", "lane3", "footer", True))

    def test_lane_unset_footer_is_unset_not_lane1(self):
        [event] = self._events("## Note\n" + _FOOT.format(kind="discussion", by="LANE-unset"))
        self.assertEqual(event["actor"], "unset")

    def test_token_era_gate_emits_one_event(self):
        body = "## L3P H1\n## Lane 3 Gate Results\n" + _FOOT.format(kind="gate-result", by="LANE3")
        self.assertEqual(self._types(body), ["gate.pass"])

    def test_each_edit_and_version_is_its_own_row(self):
        import eras
        body = "## Lane 3 Gate Results — H1\n**Verdict:** PASS"
        first = self._events(body)[0]
        edited = self._events(body, updated_at="2026-10-01T00:00:00Z")[0]
        self.assertEqual(edited["attrs"]["edited_at"], "2026-10-01T00:00:00Z")
        self.assertEqual(first["attrs"]["comment_id"], edited["attrs"]["comment_id"])
        with tempfile.TemporaryDirectory() as store, mock.patch.dict(os.environ, {emit.STORE_ENV: store}):
            self.assertEqual(emit.emit([first])["written"], 1)
            self.assertEqual(emit.emit([edited])["written"], 1)
            with mock.patch.object(eras, "EXTRACTOR_VERSION", "threads-next"):
                self.assertEqual(emit.emit(self._events(body))["written"], 1)


_FOOT = "<!-- l1-post v1; kind={kind}; posted-by={by}; body-sha256=" + "b" * 64 + " -->"


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

    def test_corrupt_cache_entry_is_a_miss_not_a_permanent_failure(self):
        cache = xt._cache_dir()
        key = hashlib.sha256("slot\x1fr".encode()).hexdigest()
        (cache / f"{key}.json").write_text("{torn")
        ok = SimpleNamespace(returncode=0, stdout='HTTP/2.0 200 OK\r\nEtag: "e"\r\n\r\n[7]', stderr="")
        with mock.patch.object(xt.subprocess, "run", mock.Mock(return_value=ok)) as run:
            self.assertEqual(xt.rest_get("r"), [[7]])
        self.assertNotIn("If-None-Match", " ".join(run.call_args.args[0]))

    def test_full_cached_last_page_is_followed_after_a_304(self):
        full = json.dumps(list(range(xt.PER_PAGE)))
        ok = 'HTTP/2.0 200 OK\r\nEtag: "a"\r\n\r\n' + full
        empty = 'HTTP/2.0 200 OK\r\nEtag: "z"\r\n\r\n[]'
        new = 'HTTP/2.0 200 OK\r\nEtag: "n"\r\n\r\n[101]'
        not_mod = "HTTP/2.0 304 Not Modified\r\n\r\n"
        outs = [ok, empty, not_mod, new]
        run = mock.Mock(side_effect=[SimpleNamespace(returncode=0, stdout=o, stderr="") for o in outs])
        with mock.patch.object(xt.subprocess, "run", run):
            self.assertEqual(len(xt.rest_get("r?per_page=100")), 2)
            pages = xt.rest_get("r?per_page=100")
        self.assertEqual(pages[-1], [101])
        self.assertIn("r?per_page=100&page=2", run.call_args_list[3].args[0])

    def test_prune_drops_stale_entries_and_orphan_temps(self):
        cache = xt._cache_dir()
        stale, orphan, live = cache / "old.json", cache / "x.tmp", cache / "new.json"
        for f in (stale, orphan, live):
            f.write_text("{}")
        past = time.time() - 40 * 86400
        os.utime(stale, (past, past))
        os.utime(orphan, (time.time() - 7200,) * 2)
        xt._prune(cache)
        self.assertEqual(sorted(f.name for f in cache.iterdir() if f.name in {"old.json", "x.tmp", "new.json"}),
                         ["new.json"])
        self.assertEqual(oct(cache.stat().st_mode & 0o777), "0o700")

    def test_http_error_raises(self):
        run = mock.Mock(return_value=SimpleNamespace(returncode=1, stdout="HTTP/2.0 404 Not Found\r\n\r\n{}", stderr="nf"))
        with mock.patch.object(xt.subprocess, "run", run), self.assertRaises(RuntimeError):
            xt.rest_get("r")


class QuotaTests(unittest.TestCase):
    def test_window_reset_mid_run_is_unmeasurable_not_negative(self):
        projects = [SimpleNamespace(name="h", repo="o/h", account="acct")]
        with mock.patch.object(xt, "extract_repo", return_value={"repo": "o/h", "issues": 2}):
            [out] = xt.run(projects, None, mock.Mock(), mock.Mock(side_effect=[120, 4940]))
        self.assertIsNone(out["quota_cost"])
        self.assertIsNone(out["quota_per_100_issues"])
        self.assertIn("reset", out["quota_note"])


if __name__ == "__main__":
    unittest.main()
