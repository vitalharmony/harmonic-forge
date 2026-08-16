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


class FetchIssueTierTests(unittest.TestCase):
    """harmonic-forge#257: Tier is the only source. The legacy Estimate
    fallback was retired with the field itself (hrse#966)."""

    def _run_returning(self, nodes):
        payload = {"data": {"repository": {"issue": {"projectItems": {"nodes": nodes}}}}}
        return MagicMock(return_value=MagicMock(returncode=0, stdout=json.dumps(payload), stderr=""))

    def _node(self, project_number, tier=None):
        return {
            "project": {"number": project_number},
            "tier": None if tier is None else {"name": tier},
        }

    def test_tier_is_read(self):
        run = self._run_returning([self._node(1, tier="fast")])
        self.assertEqual(cache.fetch_issue_tier("a/b", 1, "1", run=run), "fast")

    def test_unset_tier_is_none(self):
        run = self._run_returning([self._node(1)])
        self.assertIsNone(cache.fetch_issue_tier("a/b", 1, "1", run=run))

    def test_query_no_longer_requests_estimate(self):
        """The field is deleted from both boards; asking for it would be a
        request for something that cannot exist."""
        self.assertNotIn("Estimate", cache._TIER_QUERY)

    def test_wrong_board_ignored(self):
        run = self._run_returning([self._node(3, tier="deep")])
        self.assertIsNone(cache.fetch_issue_tier("a/b", 1, "1", run=run))

    def test_graphql_errors_raise(self):
        run = MagicMock(return_value=MagicMock(
            returncode=0, stdout=json.dumps({"data": None, "errors": [{"message": "nope"}]}), stderr=""))
        with self.assertRaises(cache.GhItemListError):
            cache.fetch_issue_tier("a/b", 1, "1", run=run)

