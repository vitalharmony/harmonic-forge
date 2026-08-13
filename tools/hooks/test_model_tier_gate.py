#!/usr/bin/env python3
"""Unit tests for model_tier_gate.py's board-fetch caching (harmonic-forge#203).
All subprocess/gh calls mocked, no live gh/API calls.
Run: python3 tools/hooks/test_model_tier_gate.py"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import model_tier_gate as m


def _completed(stdout="", returncode=0):
    class R:
        pass
    r = R()
    r.stdout = stdout
    r.returncode = returncode
    return r


ITEMS_JSON = json.dumps({"items": [
    {"content": {"number": 999}, "estimate": 8},
]})


class TestCachedItemList(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_second_call_within_ttl_does_not_hit_gh_again(self):
        """The bug this issue exists to fix: every single Edit/Write/
        apply_patch call re-ran `gh project item-list --limit 1000` from
        scratch -- the single most expensive GraphQL call observed live,
        confirmed to fully drain a 5000-point quota in a handful of calls."""
        call_count = 0

        def fake_run(cmd):
            nonlocal call_count
            call_count += 1
            return _completed(ITEMS_JSON)

        with patch("model_tier_gate._run", side_effect=fake_run):
            items1 = m._cached_item_list("owner", "3", cache_dir=self.cache_dir, ttl=120)
            items2 = m._cached_item_list("owner", "3", cache_dir=self.cache_dir, ttl=120)

        self.assertEqual(call_count, 1, "second call within TTL must not re-invoke gh")
        self.assertEqual(items1, items2)
        self.assertEqual(items1[0]["content"]["number"], 999)

    def test_call_after_ttl_expiry_refetches(self):
        call_count = 0

        def fake_run(cmd):
            nonlocal call_count
            call_count += 1
            return _completed(ITEMS_JSON)

        with patch("model_tier_gate._run", side_effect=fake_run):
            m._cached_item_list("owner", "3", cache_dir=self.cache_dir, ttl=0.05)
            time.sleep(0.1)
            m._cached_item_list("owner", "3", cache_dir=self.cache_dir, ttl=0.05)

        self.assertEqual(call_count, 2, "cache must expire and refetch after TTL")

    def test_different_owner_number_keys_are_independent(self):
        def fake_run(cmd):
            return _completed(ITEMS_JSON)

        with patch("model_tier_gate._run", side_effect=fake_run):
            m._cached_item_list("owner", "1", cache_dir=self.cache_dir, ttl=120)
            m._cached_item_list("owner", "3", cache_dir=self.cache_dir, ttl=120)

        cached_files = list(self.cache_dir.glob("*.json"))
        self.assertEqual(len(cached_files), 2, "different boards must not share a cache entry")

    def test_gh_failure_does_not_cache_and_returns_none(self):
        with patch("model_tier_gate._run", return_value=_completed("", returncode=1)):
            result = m._cached_item_list("owner", "3", cache_dir=self.cache_dir, ttl=120)
        self.assertIsNone(result)
        self.assertEqual(list(self.cache_dir.glob("*.json")), [])

    def test_resolve_tier_uses_cache_across_two_calls(self):
        """End-to-end: two resolve_tier() calls (simulating two
        consecutive hook invocations, i.e. two separate processes sharing
        the same on-disk cache) only hit gh once."""
        call_count = 0

        def fake_run(cmd):
            nonlocal call_count
            if cmd[:3] == ["gh", "project", "item-list"]:
                call_count += 1
                return _completed(ITEMS_JSON)
            return _completed("")

        with patch("model_tier_gate._run", side_effect=fake_run), \
             patch("model_tier_gate._CACHE_DIR", self.cache_dir), \
             patch("model_tier_gate.resolve_project_board", return_value=("owner", "3")):
            e1 = m.resolve_tier("/some/cwd", 999)
            e2 = m.resolve_tier("/some/cwd", 999)

        # ITEMS_JSON carries estimate=8 and no Tier -> legacy fallback -> "deep",
        # preserving the old `>= THRESHOLD` escalation boundary exactly.
        self.assertEqual(e1, "deep")
        self.assertEqual(e2, "deep")
        self.assertEqual(call_count, 1)


class TierResolutionTests(unittest.TestCase):
    """harmonic-forge#257: Tier wins when present; the legacy numeric Estimate
    is the fallback while both fields coexist."""

    def _resolve(self, item):
        with patch.object(m, "_cached_item_list", return_value=[item]), \
             patch.object(m, "resolve_project_board", return_value=("owner", "3")):
            return m.resolve_tier("/cwd", 999)

    def _item(self, **kw):
        return {"content": {"number": 999}, **kw}

    def test_tier_field_wins_over_estimate(self):
        self.assertEqual(self._resolve(self._item(tier="fast", estimate=13)), "fast")

    def test_tier_is_normalised(self):
        self.assertEqual(self._resolve(self._item(tier="  DEEP ")), "deep")

    def test_estimate_8_maps_to_deep_not_standard(self):
        """The boundary that must not move: 8 escalated before the rename."""
        self.assertEqual(self._resolve(self._item(estimate=8)), "deep")

    def test_estimate_13_maps_to_deep(self):
        self.assertEqual(self._resolve(self._item(estimate=13)), "deep")

    def test_estimate_5_maps_to_standard(self):
        self.assertEqual(self._resolve(self._item(estimate=5)), "standard")

    def test_estimate_3_maps_to_fast(self):
        self.assertEqual(self._resolve(self._item(estimate=3)), "fast")

    def test_neither_field_is_none_which_means_allow(self):
        self.assertIsNone(self._resolve(self._item()))

    def test_escalating_tiers_is_deep_only(self):
        self.assertEqual(m.ESCALATING_TIERS, frozenset({"deep"}))


if __name__ == "__main__":
    unittest.main()
