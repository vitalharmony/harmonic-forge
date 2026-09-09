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


if __name__ == "__main__":
    unittest.main(verbosity=2)
