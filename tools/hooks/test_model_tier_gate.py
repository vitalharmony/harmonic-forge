#!/usr/bin/env python3
"""Unit tests for model_tier_gate.py's board-fetch caching (harmonic-forge#203).
All subprocess/gh calls mocked, no live gh/API calls.
Run: python3 tools/hooks/test_model_tier_gate.py"""

import json
import os
import shutil
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


class IssueTargetTests(unittest.TestCase):
    def _target(self, branch, cwd="/repo"):
        with patch.object(m, "_run", return_value=_completed(branch + "\n")):
            return m.resolve_issue_target(cwd)

    def test_documented_and_cross_repo_conventions(self):
        cases = {
            "lane2/hrse-1099-cutover": (1099, "hrse"),
            "tooling/hrse875-sweep-tc-fallback": (875, "hrse"),
            "feat/367-model-tier": (367, None),
            "feat/h1209-l1-issue-amend-mode": (1209, "h"),
            "feat/f318-f321-gemini-lane-wiring": (318, "f"),
        }
        for branch, expected in cases.items():
            with self.subTest(branch=branch):
                self.assertEqual(self._target(branch), expected)

    def test_detached_hrse_worktree_is_a_known_target(self):
        with patch.object(m, "_run", return_value=_completed("")):
            self.assertEqual(m.resolve_issue_target("/tmp/hrse2-1099-impl"), (1099, "hrse"))

    def test_false_positive_shapes_stay_unmatched(self):
        for branch in ("fix/lane3-worktree-staleness-warning", "docs/adr-026-federation"):
            with self.subTest(branch=branch):
                self.assertIsNone(self._target(branch))


class ModelTierFamilies(unittest.TestCase):
    """harmonic-forge#314: the high-tier match was `"opus" in model`, so every
    non-Opus family — including Fable, which is *above* Opus — was treated as
    low-tier and told to downgrade on exactly the work that needs capability."""

    def _transcript(self, model):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        path = root / "transcript.jsonl"
        path.write_text(
            json.dumps({"message": {"role": "user"}}) + "\n"
            + json.dumps({"message": {"model": model}}) + "\n"
        )
        return {"transcript_path": str(path)}

    def test_fable_satisfies_deep(self):
        """The regression this issue was filed for — currently False."""
        self.assertTrue(m.required_tier_met(self._transcript("claude-fable-5"), True))

    def test_opus_still_satisfies_deep(self):
        self.assertTrue(m.required_tier_met(self._transcript("claude-opus-5"), True))

    def test_sonnet_does_not_satisfy_deep(self):
        self.assertFalse(m.required_tier_met(self._transcript("claude-sonnet-5"), True))

    def test_haiku_does_not_satisfy_deep(self):
        self.assertFalse(m.required_tier_met(self._transcript("claude-haiku-4-5-20251001"), True))

    def test_low_tier_model_is_fine_when_deep_not_required(self):
        self.assertTrue(m.required_tier_met(self._transcript("claude-sonnet-5"), False))

    def test_unresolvable_model_fails_open(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        path = root / "t.jsonl"
        path.write_text(json.dumps({"message": {"role": "user"}}) + "\n")
        self.assertTrue(m.required_tier_met({"transcript_path": str(path)}, True))

    def test_codex_sol_satisfies_deep_unchanged(self):
        self.assertTrue(m.required_tier_met({"model": "gpt-5.6-sol"}, True))

    def test_codex_terra_does_not_satisfy_deep_unchanged(self):
        self.assertFalse(m.required_tier_met({"model": "gpt-5.6-terra"}, True))


class TailRead(unittest.TestCase):
    """harmonic-forge#314 C4: the whole transcript was read into memory on
    every Edit/Write call to use only the last model-bearing line."""

    def test_finds_model_near_the_end_without_reading_the_whole_file(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        path = root / "big.jsonl"
        filler = json.dumps({"message": {"role": "user", "pad": "x" * 2000}})
        with path.open("w") as handle:
            for _ in range(3000):          # ~6 MB of preamble
                handle.write(filler + "\n")
            handle.write(json.dumps({"message": {"model": "claude-fable-5"}}) + "\n")
        self.assertGreater(path.stat().st_size, 4 << 20)

        real_open = open
        opened = {}

        def counting_open(*args, **kwargs):
            handle = real_open(*args, **kwargs)
            opened["handle"] = handle
            return handle

        with patch("builtins.open", counting_open):
            self.assertEqual(m.resolve_claude_model(str(path)), "claude-fable-5")

    def test_missing_file_returns_none(self):
        self.assertIsNone(m.resolve_claude_model("/nonexistent/transcript.jsonl"))

    def test_model_beyond_the_scan_budget_falls_open(self):
        """A model line buried past max_bytes is not found — same outcome as a
        transcript with no model line, at bounded cost rather than unbounded."""
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        path = root / "buried.jsonl"
        filler = json.dumps({"message": {"role": "user", "pad": "y" * 2000}})
        with path.open("w") as handle:
            handle.write(json.dumps({"message": {"model": "claude-opus-5"}}) + "\n")
            for _ in range(3000):
                handle.write(filler + "\n")
        self.assertIsNone(m.resolve_claude_model(str(path)))


if __name__ == "__main__":
    unittest.main()
