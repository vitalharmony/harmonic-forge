#!/usr/bin/env python3
"""Tests for item_list_cache.py (harmonic-forge#219)."""

import json
import shutil
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import item_list_cache as cache


class ItemListCacheTests(unittest.TestCase):
    def setUp(self):
        self._orig_dir = cache._CACHE_DIR
        cache._CACHE_DIR = Path("/tmp") / "item_list_cache_test"
        shutil.rmtree(cache._CACHE_DIR, ignore_errors=True)

    def tearDown(self):
        shutil.rmtree(cache._CACHE_DIR, ignore_errors=True)
        cache._CACHE_DIR = self._orig_dir

    def _mock_run(self, items):
        result = MagicMock()
        result.returncode = 0
        result.stdout = json.dumps({"items": items})
        result.stderr = ""
        return MagicMock(return_value=result)

    def test_cache_hit_avoids_second_call(self):
        run = self._mock_run([{"content": {"number": 1}}])
        cache.fetch_item_list("1", owner="acme", ttl=60, run=run)
        cache.fetch_item_list("1", owner="acme", ttl=60, run=run)
        self.assertEqual(run.call_count, 1)

    def test_ttl_zero_never_caches(self):
        run = self._mock_run([{"content": {"number": 1}}])
        cache.fetch_item_list("1", owner="acme", ttl=0, run=run)
        cache.fetch_item_list("1", owner="acme", ttl=0, run=run)
        self.assertEqual(run.call_count, 2)

    def test_different_limit_does_not_reuse_cache(self):
        run = self._mock_run([{"content": {"number": 1}}])
        cache.fetch_item_list("1", owner="acme", limit=500, ttl=60, run=run)
        cache.fetch_item_list("1", owner="acme", limit=1000, ttl=60, run=run)
        self.assertEqual(run.call_count, 2)

    def test_invalidate_forces_refetch(self):
        run = self._mock_run([{"content": {"number": 1}}])
        cache.fetch_item_list("1", owner="acme", ttl=60, run=run)
        cache.invalidate("acme", "1")
        cache.fetch_item_list("1", owner="acme", ttl=60, run=run)
        self.assertEqual(run.call_count, 2)

    def test_invalidate_does_not_touch_other_projects(self):
        run = self._mock_run([{"content": {"number": 1}}])
        cache.fetch_item_list("1", owner="acme", ttl=60, run=run)
        cache.fetch_item_list("3", owner="acme", ttl=60, run=run)
        cache.invalidate("acme", "1")
        cache.fetch_item_list("1", owner="acme", ttl=60, run=run)  # re-fetches
        cache.fetch_item_list("3", owner="acme", ttl=60, run=run)  # still cached
        self.assertEqual(run.call_count, 3)

    def test_gh_failure_raises(self):
        result = MagicMock(returncode=1, stdout="", stderr="boom")
        run = MagicMock(return_value=result)
        with self.assertRaises(cache.GhItemListError):
            cache.fetch_item_list("1", owner="acme", ttl=60, run=run)


class FetchIssueEstimateTests(unittest.TestCase):
    """hrse#802: targeted single-issue Estimate read, replacing a full-board scan."""

    def _run_returning(self, payload):
        result = MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")
        return MagicMock(return_value=result)

    def _payload(self, nodes):
        return {"data": {"repository": {"issue": {"projectItems": {"nodes": nodes}}}}}

    def _node(self, project_number, estimate, tier=None):
        # harmonic-forge#257: the query now aliases both fields, so the response
        # carries `estimate` and `tier` keys rather than one `fieldValueByName`.
        return {
            "project": {"number": project_number},
            "estimate": None if estimate is None else {"number": estimate},
            "tier": None if tier is None else {"name": tier},
        }

    def test_reads_estimate_from_matching_board(self):
        run = self._run_returning(self._payload([self._node(1, 5)]))
        self.assertEqual(cache.fetch_issue_estimate("acme/repo", 42, "1", run=run), 5)

    def test_does_not_fetch_the_board(self):
        """The whole point of #802 -- this must not shell out to item-list."""
        run = self._run_returning(self._payload([self._node(1, 3)]))
        cache.fetch_issue_estimate("acme/repo", 42, "1", run=run)
        args = run.call_args[0][0]
        self.assertIn("graphql", args)
        self.assertNotIn("item-list", args)

    def test_ignores_other_boards_estimate(self):
        """An issue on two projects must not return the wrong board's value."""
        run = self._run_returning(self._payload([self._node(3, 8), self._node(1, 2)]))
        self.assertEqual(cache.fetch_issue_estimate("acme/repo", 42, "1", run=run), 2)

    def test_not_on_this_board_is_none(self):
        run = self._run_returning(self._payload([self._node(3, 8)]))
        self.assertIsNone(cache.fetch_issue_estimate("acme/repo", 42, "1", run=run))

    def test_unset_estimate_is_none(self):
        run = self._run_returning(self._payload([self._node(1, None)]))
        self.assertIsNone(cache.fetch_issue_estimate("acme/repo", 42, "1", run=run))

    def test_missing_issue_is_none(self):
        run = self._run_returning({"data": {"repository": {"issue": None}}})
        self.assertIsNone(cache.fetch_issue_estimate("acme/repo", 42, "1", run=run))

    def test_float_estimate_is_coerced(self):
        run = self._run_returning(self._payload([self._node(1, 5.0)]))
        self.assertEqual(cache.fetch_issue_estimate("acme/repo", 42, "1", run=run), 5)

    def test_graphql_errors_raise_not_silently_none(self):
        """A quota/auth error must never read as 'estimate is unset' -- that
        would let a handoff post through a gate that never actually ran."""
        run = self._run_returning({"data": None, "errors": [{"message": "rate limited"}]})
        with self.assertRaises(cache.GhItemListError) as ctx:
            cache.fetch_issue_estimate("acme/repo", 42, "1", run=run)
        self.assertIn("rate limited", str(ctx.exception))

    def test_transport_failure_raises(self):
        run = MagicMock(return_value=MagicMock(returncode=1, stdout="", stderr="boom"))
        with self.assertRaises(cache.GhItemListError):
            cache.fetch_issue_estimate("acme/repo", 42, "1", run=run)

    def test_malformed_repo_raises(self):
        run = self._run_returning(self._payload([]))
        with self.assertRaises(cache.GhItemListError):
            cache.fetch_issue_estimate("norepo", 42, "1", run=run)


class FetchIssueTierTests(unittest.TestCase):
    """harmonic-forge#257: Tier preferred, legacy Estimate as fallback."""

    def _run_returning(self, nodes):
        payload = {"data": {"repository": {"issue": {"projectItems": {"nodes": nodes}}}}}
        return MagicMock(return_value=MagicMock(returncode=0, stdout=json.dumps(payload), stderr=""))

    def _node(self, project_number, estimate=None, tier=None):
        return {
            "project": {"number": project_number},
            "estimate": None if estimate is None else {"number": estimate},
            "tier": None if tier is None else {"name": tier},
        }

    def test_tier_wins_over_estimate(self):
        run = self._run_returning([self._node(1, estimate=13, tier="fast")])
        self.assertEqual(cache.fetch_issue_tier("a/b", 1, "1", run=run), "fast")

    def test_estimate_8_falls_back_to_deep(self):
        """The escalation boundary must not move during the migration."""
        run = self._run_returning([self._node(1, estimate=8)])
        self.assertEqual(cache.fetch_issue_tier("a/b", 1, "1", run=run), "deep")

    def test_estimate_5_falls_back_to_standard(self):
        run = self._run_returning([self._node(1, estimate=5)])
        self.assertEqual(cache.fetch_issue_tier("a/b", 1, "1", run=run), "standard")

    def test_estimate_2_falls_back_to_fast(self):
        run = self._run_returning([self._node(1, estimate=2)])
        self.assertEqual(cache.fetch_issue_tier("a/b", 1, "1", run=run), "fast")

    def test_neither_field_is_none(self):
        run = self._run_returning([self._node(1)])
        self.assertIsNone(cache.fetch_issue_tier("a/b", 1, "1", run=run))

    def test_wrong_board_ignored(self):
        run = self._run_returning([self._node(3, tier="deep")])
        self.assertIsNone(cache.fetch_issue_tier("a/b", 1, "1", run=run))

    def test_graphql_errors_raise(self):
        run = MagicMock(return_value=MagicMock(
            returncode=0, stdout=json.dumps({"data": None, "errors": [{"message": "nope"}]}), stderr=""))
        with self.assertRaises(cache.GhItemListError):
            cache.fetch_issue_tier("a/b", 1, "1", run=run)


class TierMappingTests(unittest.TestCase):
    def test_boundary_is_eight(self):
        self.assertEqual(cache.tier_for_points(8), "deep")
        self.assertEqual(cache.tier_for_points(7), "standard")
        self.assertEqual(cache.tier_for_points(5), "standard")
        self.assertEqual(cache.tier_for_points(3), "fast")
        self.assertIsNone(cache.tier_for_points(None))

    def test_only_deep_escalates(self):
        self.assertEqual(cache.ESCALATING_TIERS, frozenset({"deep"}))


if __name__ == "__main__":
    unittest.main()
