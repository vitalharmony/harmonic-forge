#!/usr/bin/env python3
"""Tests for item_list_cache.py (harmonic-forge#219)."""

import os
import io
import contextlib
import json
import shutil
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import item_list_cache as cache
import tempfile


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
            # harmonic-forge#468: the query aliases the field value as `value`
            # now that it reads any field, not only Tier.
            "value": None if tier is None else {"name": tier},
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
        self.assertNotIn("Estimate", cache._FIELD_QUERY)

    def test_query_reads_every_field_value_type(self):
        """harmonic-forge#468 AC4. The Tier-only version read single-selects
        alone — and `Sequence`, the field the 2026-09-04 quota incident was
        actually asking about, is a NUMBER field. A single-select-only read
        would answer None for the very question that caused the burn."""
        for fragment in (
            "ProjectV2ItemFieldSingleSelectValue",
            "ProjectV2ItemFieldTextValue",
            "ProjectV2ItemFieldNumberValue",
            "ProjectV2ItemFieldDateValue",
        ):
            self.assertIn(fragment, cache._FIELD_QUERY)

    def test_wrong_board_ignored(self):
        run = self._run_returning([self._node(3, tier="deep")])
        self.assertIsNone(cache.fetch_issue_tier("a/b", 1, "1", run=run))

    def test_graphql_errors_raise(self):
        run = MagicMock(return_value=MagicMock(
            returncode=0, stdout=json.dumps({"data": None, "errors": [{"message": "nope"}]}), stderr=""))
        with self.assertRaises(cache.GhItemListError):
            cache.fetch_issue_tier("a/b", 1, "1", run=run)



class MandatedPathDefaults(unittest.TestCase):
    """harmonic-forge#468 AC1 — the defaults are the cheap path now.

    The old contract was `limit=5000, ttl=0`: pull every item, do not read the
    cache. That was the *default*, and it zeroed the GraphQL quota twice with
    the correct guidance already written in a comment beside it.
    """

    def test_the_general_reads_default_to_a_real_ttl(self):
        import inspect
        for fn in (cache.get_board_items, cache.fetch_full_board, cache.fetch_issue_field):
            with self.subTest(fn=fn.__name__):
                self.assertEqual(
                    inspect.signature(fn).parameters["ttl"].default,
                    cache.DEFAULT_TTL_SECONDS,
                    f"{fn.__name__} still defaults to a cache-bypassing ttl",
                )

    def test_fetch_issue_tier_keeps_ttl_zero_deliberately(self):
        """The one exception, and it is not an oversight: the model-tier gate
        reads a tier to decide whether to block a tool call, and a cached
        "no tier" from before the operator set one would gate wrongly for the
        whole window."""
        import inspect
        self.assertEqual(inspect.signature(cache.fetch_issue_tier).parameters["ttl"].default, 0)

    def test_the_expensive_entry_point_is_named_for_its_cost(self):
        """AC2. `fetch_item_list` said nothing about what it costs."""
        self.assertTrue(hasattr(cache, "fetch_full_board"))
        self.assertTrue(hasattr(cache, "get_board_items"))

    def test_the_old_name_survives_as_an_alias_with_the_old_behaviour(self):
        """Ratified: kept, not deleted. It has no production callers, but
        ad-hoc scripts point at it — and it must keep its OLD ttl=0 semantics
        so an existing caller's behaviour does not change underneath it."""
        import inspect
        self.assertTrue(callable(cache.fetch_item_list))
        self.assertIn("Deprecated", inspect.getdoc(cache.fetch_item_list))


class FullScanCooldown(unittest.TestCase):
    """AC6 — repeat full scans within a short window are refused.

    The 2026-09-04 incident was two whole-board pulls twelve minutes apart.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _run_ok(self, n=1):
        return MagicMock(return_value=MagicMock(
            returncode=0, stdout=json.dumps({"items": [{"id": f"i{i}"} for i in range(n)]}),
        ))

    def test_a_second_full_scan_in_the_window_is_refused(self):
        run = self._run_ok()
        cache.fetch_full_board("1", run=run, ttl=0, cache_dir=self.dir, force=True)
        # force=True on the first call writes nothing; seed the marker the way a
        # cached scan would.
        (self.dir / "vitalharmony_1_5000.json").write_text("[]")
        with self.assertRaises(cache.FullScanTooSoon):
            cache.fetch_full_board("1", run=run, ttl=0, cache_dir=self.dir)

    def test_force_overrides_the_cooldown(self):
        (self.dir / "vitalharmony_1_5000.json").write_text("[]")
        run = self._run_ok()
        cache.fetch_full_board("1", run=run, ttl=0, cache_dir=self.dir, force=True)

    def test_the_refusal_names_the_cheaper_alternatives(self):
        """A guard that only says no sends the caller looking for a way around
        it. This one has to name the path it is protecting."""
        (self.dir / "vitalharmony_1_5000.json").write_text("[]")
        with self.assertRaises(cache.FullScanTooSoon) as caught:
            cache.fetch_full_board("1", run=self._run_ok(), ttl=0, cache_dir=self.dir)
        message = str(caught.exception)
        self.assertIn("fetch_issue_field", message)
        self.assertIn("get_board_items", message)


class CurrencyCheck(unittest.TestCase):
    """AC3 — currency is checked, not assumed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.cache_file = self.dir / "vitalharmony_1_5000.json"
        self.stamp = self.dir / "vitalharmony_1_5000.currency.json"

    def _currency(self, updated="2026-09-05T01:02:45Z", count=905):
        return MagicMock(returncode=0, stdout=json.dumps(
            {"data": {"user": {"projectV2": {
                "updatedAt": updated, "items": {"totalCount": count}}}}}))

    def test_an_unchanged_board_serves_the_cache_at_any_age(self):
        """The point of a currency check over a TTL: an untouched board does
        not need re-scanning just because ten minutes elapsed."""
        self.cache_file.write_text(json.dumps([{"id": "cached"}]))
        self.stamp.write_text(json.dumps({"updated_at": "2026-09-05T01:02:45Z", "total_count": 905}))
        os.utime(self.cache_file, (0, 0))  # ancient — TTL alone would refetch
        run = MagicMock(side_effect=[self._currency()])
        items = cache.get_board_items("1", run=run, cache_dir=self.dir)
        self.assertEqual(items, [{"id": "cached"}])
        self.assertEqual(run.call_count, 1, "an unchanged board must cost ONE probe")

    def test_a_changed_board_refetches(self):
        self.cache_file.write_text(json.dumps([{"id": "stale"}]))
        self.stamp.write_text(json.dumps({"updated_at": "2026-09-05T00:00:00Z", "total_count": 900}))
        os.utime(self.cache_file, (0, 0))
        run = MagicMock(side_effect=[
            self._currency(),
            MagicMock(returncode=0, stdout=json.dumps({"items": [{"id": "fresh"}]})),
            self._currency(),
        ])
        self.assertEqual(cache.get_board_items("1", run=run, cache_dir=self.dir), [{"id": "fresh"}])

    def test_a_failed_probe_degrades_to_ttl_and_says_so(self):
        """harmonic-forge#440's lesson: a check that silently stopped checking
        is the failure mode this whole issue is about."""
        self.cache_file.write_text(json.dumps([{"id": "cached"}]))
        run = MagicMock(side_effect=[MagicMock(returncode=1, stdout="")])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            items = cache.get_board_items("1", run=run, cache_dir=self.dir)
        self.assertEqual(items, [{"id": "cached"}], "fresh cache still served on TTL")
        self.assertIn("currency probe unavailable", err.getvalue())

    def test_the_probe_costs_one_query_for_the_board(self):
        """It reads `items(first: 1) { totalCount }` — a count, not the rows."""
        self.assertIn("items(first: 1)", cache._BOARD_CURRENCY_QUERY)
        self.assertIn("totalCount", cache._BOARD_CURRENCY_QUERY)
        self.assertIn("updatedAt", cache._BOARD_CURRENCY_QUERY)
