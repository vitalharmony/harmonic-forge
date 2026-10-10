#!/usr/bin/env python3
"""Tests for belt_mechanics (harmonic-forge#518).

The AC5 case is deliberately **live**: at the time of writing the
`harmonicarchitect` slot authenticates as `vitalharmony`, which is a genuinely
broken account rather than a mock. The operator ratified verifying against it
before repairing it, because the fixture disappears on re-authentication. That
test skips rather than fails once the account is fixed — a repaired account must
not look like a regression.
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from belt_mechanics import (  # noqa: E402
    CallCounter,
    IdentityMismatch,
    SeenSet,
    TickLog,
    Watermarks,
    _is_graphql,
    assert_identity,
    list_accounts,
    query_since,
    session_lock,
)

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


class TestGraphQLDetection(unittest.TestCase):
    """AC16. The counter is what makes this continuous rather than a one-time
    grep, so the classification itself has to be right."""

    def test_rest_api_calls_are_rest(self):
        self.assertFalse(_is_graphql(["api", "repos/o/r/issues?state=open"]))
        self.assertFalse(_is_graphql(["api", "repos/o/r/issues/1/comments"]))

    def test_explicit_graphql_api_call_is_graphql(self):
        self.assertTrue(_is_graphql(["api", "graphql", "-f", "query=..."]))

    def test_graphql_backed_subcommands(self):
        """`gh search issues` is the one live in the belt's own tooling today."""
        for sub in ("search", "issue", "pr", "project"):
            self.assertTrue(_is_graphql([sub, "list"]), sub)

    def test_counter_separates_them(self):
        c = CallCounter()
        c.record(["api", "repos/o/r/issues"])
        c.record(["search", "issues"])
        c.record(["api", "repos/o/r/issues/1/comments"])
        self.assertEqual((c.calls_rest, c.calls_graphql), (2, 1))


class TestQuerySince(unittest.TestCase):
    """AC11. Watermark is the floor; overlap covers the seam it cannot."""

    def test_no_watermark_uses_the_overlap_window(self):
        self.assertEqual(query_since(None, 90, NOW), NOW - timedelta(minutes=90))

    def test_a_recent_watermark_is_widened_by_the_overlap(self):
        recent = NOW - timedelta(minutes=5)
        self.assertEqual(query_since(recent, 90, NOW), NOW - timedelta(minutes=90))

    def test_a_stalled_watermark_is_never_skipped_past(self):
        """The failure this exists to prevent: a repo whose call failed for
        hours must be re-read from where it stopped, not from now-K."""
        stalled = NOW - timedelta(hours=6)
        self.assertEqual(query_since(stalled, 90, NOW), stalled)


class TestWatermarks(unittest.TestCase):
    def test_each_repo_is_independent(self):
        """The settled decision. One repo's failure must not move another's."""
        with tempfile.TemporaryDirectory() as d:
            w = Watermarks(Path(d))
            w.advance("vitalharmony", "hrse", NOW)
            self.assertEqual(w.get("vitalharmony", "hrse"), NOW)
            self.assertIsNone(w.get("vitalharmony", "harmonic-forge"))

    def test_same_repo_name_under_two_accounts_does_not_collide(self):
        with tempfile.TemporaryDirectory() as d:
            w = Watermarks(Path(d))
            w.advance("vitalharmony", "shared", NOW)
            self.assertIsNone(w.get("harmonicarchitect", "shared"))


class TestSeenSet(unittest.TestCase):
    """AC12 — the distinction a bare-id file cannot make."""

    def test_primed_and_emitted_are_distinguishable(self):
        with tempfile.TemporaryDirectory() as d:
            s = SeenSet(Path(d) / "seen.tsv")
            s.prime(["100", "101"])
            s.add("200", SeenSet.EMITTED)
            self.assertEqual(s.status("100"), SeenSet.PRIMED)
            self.assertEqual(s.status("200"), SeenSet.EMITTED)
            self.assertIn("101", s)

    def test_state_survives_a_reload(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "seen.tsv"
            SeenSet(path).prime(["100"])
            self.assertEqual(SeenSet(path).status("100"), SeenSet.PRIMED)

    def test_priming_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            s = SeenSet(Path(d) / "seen.tsv")
            self.assertEqual(s.prime(["1", "2"]), 2)
            self.assertEqual(s.prime(["1", "2"]), 0)


class TestTickLog(unittest.TestCase):
    """AC17."""

    def test_a_quiet_tick_still_writes_a_record(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ticks.jsonl"
            TickLog(path=path, lane="2", trigger="monitor").write()
            rec = json.loads(path.read_text().strip())
            self.assertEqual(rec["lane"], "2")
            self.assertEqual(rec["matched"], [])

    def test_polled_but_silent_is_distinct_from_never_polled(self):
        """17b. A repo with zero events is suspicious, not clean — but only if
        the log can tell 'polled and matched nothing' from 'never polled'."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ticks.jsonl"
            log = TickLog(path=path, lane="2", trigger="loop")
            log.repo_result("vitalharmony", "cymagraph-infra", ok=True)
            log.repo_result("vitalharmony", "openclaw-projects", ok=False, error="503")
            rec = log.write()
            names = {r["repo"]: r for r in rec["repos_polled"]}
            self.assertTrue(names["cymagraph-infra"]["ok"])
            self.assertFalse(names["openclaw-projects"]["ok"])
            self.assertNotIn("hrse", names)

    def test_graphql_count_is_carried_into_the_record(self):
        with tempfile.TemporaryDirectory() as d:
            log = TickLog(path=Path(d) / "t.jsonl", lane="1", trigger="monitor")
            log.counter.record(["search", "issues"])
            self.assertEqual(log.write()["calls_graphql"], 1)

    def test_appends_rather_than_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ticks.jsonl"
            for _ in range(3):
                TickLog(path=path, lane="3", trigger="loop").write()
            self.assertEqual(len(path.read_text().strip().splitlines()), 3)


class TestSessionLock(unittest.TestCase):
    """The `HELD` branch has never fired in any observed live tick, in any lane
    — so it is exercised here rather than trusted."""

    def test_second_acquire_reports_held(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "poll.lock"
            with session_lock(path) as first:
                self.assertTrue(first.acquired)
                with session_lock(path) as second:
                    self.assertFalse(second.acquired)
                    self.assertEqual(second.state, "held")

    def test_lock_is_released_on_exit(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "poll.lock"
            with session_lock(path):
                pass
            self.assertFalse(path.exists())
            with session_lock(path) as again:
                self.assertTrue(again.acquired)

    def test_a_held_lock_is_not_removed_by_the_loser(self):
        """The bug this guards: a second tick that fails to acquire must not
        delete the holder's lock on its way out."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "poll.lock"
            with session_lock(path):
                with session_lock(path) as loser:
                    self.assertFalse(loser.acquired)
                self.assertTrue(path.exists())

    def test_a_stale_lock_is_reclaimed(self):
        import os
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "poll.lock"
            path.mkdir()
            old = (datetime.now(timezone.utc) - timedelta(minutes=30)).timestamp()
            os.utime(path, (old, old))
            with session_lock(path, stale_after_minutes=10) as lock:
                self.assertTrue(lock.acquired)
                self.assertEqual(lock.state, "reclaimed")


class TestIdentityRefusesLoudly(unittest.TestCase):
    """AC4/AC5. Empty-equals-quiet is the entire failure class."""

    def test_mismatch_raises_rather_than_returning_empty(self):
        with self.assertRaises(IdentityMismatch) as ctx:
            assert_identity("harmonicarchitect", {"harmonicarchitect": "vitalharmony"})
        self.assertIn("authenticates as", str(ctx.exception))

    def test_unconfigured_account_raises(self):
        with self.assertRaises(IdentityMismatch):
            assert_identity("leasepal", {"vitalharmony": "vitalharmony"})

    def test_unauthenticated_account_raises(self):
        with self.assertRaises(IdentityMismatch):
            assert_identity("kenekted", {"kenekted": "(not authenticated)"})

    def test_matching_identity_passes(self):
        assert_identity("vitalharmony", {"vitalharmony": "vitalharmony"})


class TestAgainstTheLiveAccounts(unittest.TestCase):
    """AC5 against the real broken fixture, ratified by the operator.

    Skips once the account is repaired — a fixed account must not read as a
    regression, and this test's job is to prove the refusal fires against a
    genuinely wrong identity, which it can only do while one exists.
    """

    def test_live_mismatch_is_refused(self):
        try:
            accounts = list_accounts()
        except Exception as exc:  # noqa: BLE001 - environment-dependent
            self.skipTest(f"gh-as unavailable: {exc}")
        broken = [a for a, who in accounts.items()
                  if who != a and who != "(not authenticated)"]
        if not broken:
            self.skipTest("no mismatched account configured — fixture repaired")
        for account in broken:
            with self.assertRaises(IdentityMismatch):
                assert_identity(account, accounts)


class RequestAccountingTests(unittest.TestCase):
    """harmonic-forge#959 AC1/AC2: `gh_as` reads the rate limit from the
    headers of the call it already makes, and returns only the body."""

    HDR = ("HTTP/2.0 200 OK\r\nX-Ratelimit-Reset: 1791671689\r\n"
           "X-Ratelimit-Resource: core\r\nX-Ratelimit-Used: 130\r\n\r\n")

    def _run(self, argv, stdout, returncode=0, check=True):
        import belt_mechanics
        from unittest.mock import patch
        calls = []

        def fake(cmd, **kw):
            calls.append(cmd)
            return type("P", (), {"returncode": returncode, "stdout": stdout, "stderr": "boom"})()

        counter = belt_mechanics.CallCounter()
        with patch("belt_mechanics.subprocess.run", side_effect=fake):
            out = belt_mechanics.gh_as("vitalharmony", argv, counter=counter, check=check)
        return out, counter, calls

    def test_the_header_block_is_stripped(self):
        out, _, _ = self._run(["api", "repos/o/r/issues/1"], self.HDR + '{"a": 1}')
        self.assertEqual(out, '{"a": 1}')

    def test_the_rate_limit_is_recorded_from_mixed_case_headers(self):
        _, counter, _ = self._run(["api", "repos/o/r/issues/1"], self.HDR + "open\n")
        self.assertEqual(counter.ratelimit,
                         {"vitalharmony": {"used": 130, "reset": 1791671689}})
        self.assertEqual(counter.http_requests, 1)

    def test_a_caller_passing_include_gets_raw_output(self):
        raw = self.HDR + "{}"
        out, counter, calls = self._run(["api", "--include", "repos/o/r"], raw)
        self.assertEqual(out, raw)
        self.assertEqual(calls[0].count("--include"), 1)

    def test_a_paginated_call_is_not_given_include(self):
        out, counter, calls = self._run(["api", "x", "--paginate"], "[]")
        self.assertNotIn("--include", calls[0])
        self.assertEqual(out, "[]")

    def test_a_redirect_strips_every_block_and_records_the_last(self):
        stdout = ("HTTP/2.0 301 Moved Permanently\r\nLocation: x\r\n\r\n"
                  "HTTP/2.0 200 OK\r\nX-Ratelimit-Used: 7\r\nX-Ratelimit-Reset: 9\r\n\r\n"
                  "open\n")
        out, counter, _ = self._run(["api", "repos/o/r/issues/1"], stdout)
        self.assertEqual(out, "open\n")
        self.assertEqual(counter.ratelimit["vitalharmony"]["used"], 7)

    def test_a_failed_call_still_raises(self):
        import belt_mechanics
        with self.assertRaises(belt_mechanics.GhAsError) as ctx:
            self._run(["api", "repos/o/r"], self.HDR + "{}", returncode=1)
        self.assertNotIn("--include", str(ctx.exception))

    def test_a_403_still_records_the_rate_limit(self):
        import belt_mechanics
        hdr = ("HTTP/2.0 403 Forbidden\r\nX-Ratelimit-Used: 5000\r\n"
               "X-Ratelimit-Reset: 9\r\n\r\n")
        from unittest.mock import patch
        counter = belt_mechanics.CallCounter()
        fake = type("P", (), {"returncode": 1, "stdout": hdr + "{}", "stderr": "rate"})()
        with patch("belt_mechanics.subprocess.run", return_value=fake):
            with self.assertRaises(belt_mechanics.GhAsError):
                belt_mechanics.gh_as("vitalharmony", ["api", "x"], counter=counter)
        self.assertEqual(counter.ratelimit["vitalharmony"]["used"], 5000)

    def test_one_subprocess_per_call(self):
        _, _, calls = self._run(["api", "repos/o/r/issues/1"], self.HDR + "{}")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
