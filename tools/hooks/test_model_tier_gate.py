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

# Shape returned by the targeted per-issue GraphQL read (harmonic-forge#250).
# `deep` is the escalating tier, so this payload exercises the branch that
# actually gates work.
TIER_QUERY_JSON = json.dumps({"data": {"repository": {"issue": {"projectItems": {
    "nodes": [{"project": {"number": 3}, "tier": {"name": "deep"}}],
}}}}})


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
        the same on-disk cache) only hit gh once.

        harmonic-forge#250 changed *which* call is made -- a targeted
        per-issue GraphQL read instead of a whole-board item-list -- but
        the cached-across-processes property is the reason the hook is
        survivable at all, so it is asserted against the new call."""
        call_count = 0

        def fake_run(cmd):
            nonlocal call_count
            if cmd[:3] == ["gh", "api", "graphql"]:
                call_count += 1
                return _completed(TIER_QUERY_JSON)
            return _completed("")

        with patch("model_tier_gate._run", side_effect=fake_run), \
             patch("model_tier_gate._CACHE_DIR", self.cache_dir), \
             patch("model_tier_gate.resolve_repo", return_value="o/r"), \
             patch("model_tier_gate.resolve_project_board", return_value=("owner", "3")):
            e1 = m.resolve_tier("/some/cwd", 999)
            e2 = m.resolve_tier("/some/cwd", 999)

        # estimate=8, no Tier -> legacy fallback -> "deep", preserving the old
        # `>= THRESHOLD` escalation boundary exactly.
        self.assertEqual(e1, "deep")
        self.assertEqual(e2, "deep")
        self.assertEqual(call_count, 1)

    def test_the_board_is_no_longer_fetched_at_all(self):
        """The whole point of #250: no `gh project item-list` on the hot path."""
        seen = []

        def fake_run(cmd):
            seen.append(cmd)
            return _completed(TIER_QUERY_JSON)

        with patch("model_tier_gate._run", side_effect=fake_run), \
             patch("model_tier_gate._CACHE_DIR", self.cache_dir), \
             patch("model_tier_gate.resolve_repo", return_value="o/r"), \
             patch("model_tier_gate.resolve_project_board", return_value=("owner", "3")):
            m.resolve_tier("/some/cwd", 999)

        self.assertFalse(
            [c for c in seen if c[:3] == ["gh", "project", "item-list"]],
            "resolve_tier must not fetch the whole board",
        )
        self.assertTrue([c for c in seen if c[:3] == ["gh", "api", "graphql"]])

    def test_lookup_failure_is_not_cached(self):
        """A quota blip must not freeze a false 'unset' for the TTL window --
        the hrse#802 lesson, applied to the write side of the cache."""
        with patch("model_tier_gate._run", return_value=_completed("", returncode=1)), \
             patch("model_tier_gate._CACHE_DIR", self.cache_dir), \
             patch("model_tier_gate.resolve_repo", return_value="o/r"), \
             patch("model_tier_gate.resolve_project_board", return_value=("owner", "3")):
            self.assertIsNone(m.resolve_tier("/some/cwd", 999))
        self.assertEqual(list(self.cache_dir.glob("tier__*.json")), [])

    def test_different_issues_do_not_share_a_cache_entry(self):
        def fake_run(cmd):
            return _completed(TIER_QUERY_JSON)

        with patch("model_tier_gate._run", side_effect=fake_run), \
             patch("model_tier_gate._CACHE_DIR", self.cache_dir), \
             patch("model_tier_gate.resolve_repo", return_value="o/r"), \
             patch("model_tier_gate.resolve_project_board", return_value=("owner", "3")):
            m.resolve_tier("/some/cwd", 999)
            m.resolve_tier("/some/cwd", 1000)

        self.assertEqual(len(list(self.cache_dir.glob("tier__*.json"))), 2)


class TierResolutionTests(unittest.TestCase):
    """What resolve_tier() is responsible for after harmonic-forge#250.

    The Tier-vs-Estimate precedence and the points->tier boundaries moved
    into item_list_cache.fetch_issue_tier / tier_for_points, and are covered
    there (test_item_list_cache.py, including tier_for_points(8) == 'deep').
    Re-asserting them through a mock here would test the mock. What is
    genuinely this function's job is delegating with the right arguments and
    failing open on every resolution failure -- so that is what is tested,
    plus one real end-to-end guard on the boundary that must never move."""

    def setUp(self):
        # Each test must get a cold cache. These all resolve the same
        # (repo, issue, board) key, so without isolation the first result is
        # served to every later test and the payloads are never read -- which
        # is exactly how this suite first went green on a wrong answer.
        self.tmpdir = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _resolve(self, payload_json, *, repo="o/r", board=("owner", "3")):
        with patch.object(m, "_run", return_value=_completed(payload_json)), \
             patch.object(m, "_CACHE_DIR", self.cache_dir), \
             patch.object(m, "resolve_repo", return_value=repo), \
             patch.object(m, "resolve_project_board", return_value=board):
            return m.resolve_tier("/cwd", 999)

    def _payload(self, tier=None, project=3):
        node = {"project": {"number": project},
                "tier": {"name": tier} if tier else None}
        return json.dumps({"data": {"repository": {"issue": {
            "projectItems": {"nodes": [node]}}}}})

    def test_deep_resolves_end_to_end(self):
        """Asserted through the real read path rather than a mocked return
        value. `deep` is the only escalating tier, so this is the branch that
        decides whether a session is gated."""
        self.assertEqual(self._resolve(self._payload(tier="deep")), "deep")

    def test_tier_field_wins_over_estimate(self):
        self.assertEqual(self._resolve(self._payload(tier="fast")), "fast")

    def test_tier_is_normalised(self):
        self.assertEqual(self._resolve(self._payload(tier="  DEEP ")), "deep")

    def test_neither_field_is_none_which_means_allow(self):
        self.assertIsNone(self._resolve(self._payload()))

    def test_targeted_read_gets_repo_issue_and_board_number(self):
        """#250's one real complication: the targeted query needs owner/name,
        which resolve_project_board does not supply."""
        seen = {}

        def fake_fetch(repo, issue_number, project_number, **kw):
            seen.update(repo=repo, issue=issue_number, project=project_number, kw=kw)
            return "fast"

        with patch.object(m._item_list_cache, "fetch_issue_tier", fake_fetch), \
             patch.object(m, "resolve_repo", return_value="vitalharmony/hrse"), \
             patch.object(m, "resolve_project_board", return_value=("vitalharmony", "1")):
            self.assertEqual(m.resolve_tier("/cwd", 966), "fast")

        self.assertEqual(seen["repo"], "vitalharmony/hrse")
        self.assertEqual(seen["issue"], 966)
        self.assertEqual(seen["project"], "1")
        self.assertGreater(seen["kw"]["ttl"], 0, "the cache must stay on the hot path")

    def test_unresolvable_repo_fails_open(self):
        with patch.object(m, "resolve_repo", return_value=None), \
             patch.object(m, "resolve_project_board", return_value=("owner", "3")):
            self.assertIsNone(m.resolve_tier("/cwd", 999))

    def test_unresolvable_board_fails_open(self):
        with patch.object(m, "resolve_project_board", return_value=None):
            self.assertIsNone(m.resolve_tier("/cwd", 999))

    def test_lookup_error_fails_open(self):
        def boom(*a, **k):
            raise m._item_list_cache.GhItemListError("quota")

        with patch.object(m._item_list_cache, "fetch_issue_tier", boom), \
             patch.object(m, "resolve_repo", return_value="o/r"), \
             patch.object(m, "resolve_project_board", return_value=("owner", "3")):
            self.assertIsNone(m.resolve_tier("/cwd", 999))

    def test_escalating_tiers_is_deep_only(self):
        self.assertEqual(m.ESCALATING_TIERS, frozenset({"deep"}))


class ResolveRepoTests(unittest.TestCase):
    """GH_REPO is read from the repo's own mise.toml for the same reason
    resolve_project_board does it: os.environ is only correct if the calling
    shell ran mise's cd-hook for this exact cwd (harmonic-forge#202)."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _resolve(self):
        with patch.object(m, "_run", return_value=_completed(str(self.root))):
            return m.resolve_repo("/cwd")

    def test_reads_gh_repo_from_mise_toml(self):
        (self.root / "mise.toml").write_text('GH_REPO = "vitalharmony/hrse"\n')
        self.assertEqual(self._resolve(), "vitalharmony/hrse")

    def test_missing_mise_toml_fails_open(self):
        self.assertIsNone(self._resolve())

    def test_mise_toml_without_gh_repo_fails_open(self):
        (self.root / "mise.toml").write_text('GH_PROJECT_NUMBER = "3"\n')
        self.assertIsNone(self._resolve())

    def test_does_not_read_os_environ(self):
        """The #202 leak: a shell with another repo's env activated."""
        (self.root / "mise.toml").write_text('GH_REPO = "vitalharmony/harmonic-forge"\n')
        with patch.dict(os.environ, {"GH_REPO": "vitalharmony/hrse"}):
            self.assertEqual(self._resolve(), "vitalharmony/harmonic-forge")


if __name__ == "__main__":
    unittest.main()
