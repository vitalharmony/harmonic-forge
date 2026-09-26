#!/usr/bin/env python3
"""Unit tests for model_tier_gate.py's board-fetch caching (harmonic-forge#203).
All subprocess/gh calls mocked, no live gh/API calls.
Run: python3 tools/hooks/test_model_tier_gate.py"""

import json
import os
import re
import shutil
import subprocess
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
    "nodes": [{"project": {"number": 3}, "value": {"name": "deep"}}],
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

        with patch("model_tier_gate.timed_run", side_effect=fake_run), \
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

        with patch("model_tier_gate.timed_run", side_effect=fake_run), \
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
        with patch("model_tier_gate.timed_run", return_value=_completed("", returncode=1)), \
             patch("model_tier_gate._CACHE_DIR", self.cache_dir), \
             patch("model_tier_gate.resolve_repo", return_value="o/r"), \
             patch("model_tier_gate.resolve_project_board", return_value=("owner", "3")):
            # harmonic-forge#656 AC6: a failed read is LOOKUP_FAILED, not None.
            self.assertIs(m.resolve_tier("/some/cwd", 999), m.LOOKUP_FAILED)
        self.assertEqual(list(self.cache_dir.glob("tier__*.json")), [])
        self.assertEqual(list(self.cache_dir.glob("field__*.json")), [])

    def test_different_issues_do_not_share_a_cache_entry(self):
        def fake_run(cmd):
            return _completed(TIER_QUERY_JSON)

        with patch("model_tier_gate.timed_run", side_effect=fake_run), \
             patch("model_tier_gate._CACHE_DIR", self.cache_dir), \
             patch("model_tier_gate.resolve_repo", return_value="o/r"), \
             patch("model_tier_gate.resolve_project_board", return_value=("owner", "3")):
            m.resolve_tier("/some/cwd", 999)
            m.resolve_tier("/some/cwd", 1000)

        # harmonic-forge#468: the prefix is `field__<name>__` now, because the
        # field name became part of the key — without it, reading `Sequence`
        # would overwrite the cached `Tier` for the same issue and the gate
        # would read a sequence number as a tier.
        self.assertEqual(len(list(self.cache_dir.glob("field__tier__*.json"))), 2)


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
        with patch.object(m, "timed_run", return_value=_completed(payload_json)), \
             patch.object(m, "_CACHE_DIR", self.cache_dir), \
             patch.object(m, "resolve_repo", return_value=repo), \
             patch.object(m, "resolve_project_board", return_value=board):
            return m.resolve_tier("/cwd", 999)

    def _payload(self, tier=None, project=3):
        # harmonic-forge#468: the query aliases the field value as `value` now
        # that it reads any field, not only Tier.
        node = {"project": {"number": project},
                "value": {"name": tier} if tier else None}
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

    def test_lookup_error_is_distinct_from_unset(self):
        """harmonic-forge#656 AC6: this test used to assert None (fail open).
        A failed read must no longer look like "no Tier set"."""
        def boom(*a, **k):
            raise m._item_list_cache.GhItemListError("HTTP 403: rate limit")

        with patch.object(m._item_list_cache, "fetch_issue_tier", boom), \
             patch.object(m, "resolve_repo", return_value="o/r"), \
             patch.object(m, "resolve_project_board", return_value=("owner", "3")):
            self.assertIs(m.resolve_tier("/cwd", 999), m.LOOKUP_FAILED)
            self.assertEqual(m.read_tier("o/r", 999, "3"),
                             (m.LOOKUP_FAILED, "HTTP 403: rate limit"))

    def test_not_an_issue_is_distinct_from_a_failed_read(self):
        """harmonic-forge#656 preclose fix 3: a PR number (GraphQL NOT_FOUND)
        is not a failed read. `read_tier` says so; the edit gate treats it as
        no Tier (allow), never LOOKUP_FAILED (deny)."""
        def not_found(*a, **k):
            raise m._item_list_cache.GhItemListError(
                "GraphQL: Could not resolve to an Issue with the number of 1833. (repository.issue)")

        with patch.object(m._item_list_cache, "fetch_issue_tier", not_found), \
             patch.object(m, "resolve_repo", return_value="o/r"), \
             patch.object(m, "resolve_project_board", return_value=("owner", "3")):
            self.assertIs(m.read_tier("o/r", 1833, "3")[0], m.NOT_AN_ISSUE)
            self.assertIsNone(m.resolve_tier("/cwd", 1833))

    def test_not_an_issue_classifier_is_narrow(self):
        self.assertTrue(m.is_not_an_issue_error(
            "Could not resolve to an Issue with the number of 5."))
        self.assertTrue(m.is_not_an_issue_error('{"type": "NOT_FOUND"}'))
        for message in ("HTTP 403: API rate limit exceeded", "gh api graphql failed",
                        "error connecting to api.github.com", "board read timed out after 4s"):
            with self.subTest(message=message):
                self.assertFalse(m.is_not_an_issue_error(message))

    def test_resolve_tier_read_is_timed(self):
        """Preclose fix 5: an untimed read hung until the registration timeout
        killed the hook, which let the edit through. Simulated: only a call
        carrying a timeout can time out; an untimed one "succeeds" with deep."""
        node = {"project": {"number": 3}, "value": {"name": "deep"}}
        body = json.dumps({"data": {"repository": {"issue": {"projectItems": {"nodes": [node]}}}}})

        def fake_subprocess_run(cmd, **kw):
            if kw.get("timeout") is not None:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=kw["timeout"])
            return _completed(body)

        with tempfile.TemporaryDirectory() as cache_dir, \
             patch("subprocess.run", side_effect=fake_subprocess_run), \
             patch.object(m, "_CACHE_DIR", Path(cache_dir)), \
             patch.object(m, "resolve_repo", return_value="o/r"), \
             patch.object(m, "resolve_project_board", return_value=("owner", "3")):
            self.assertIs(m.resolve_tier("/cwd", 999), m.LOOKUP_FAILED)

    def test_timed_run_passes_a_timeout(self):
        with patch("subprocess.run", return_value=_completed("")) as sp:
            m.timed_run(["gh"])
        self.assertEqual(sp.call_args.kwargs.get("timeout"), m._READ_TIMEOUT_SECONDS)

    def test_edit_gate_keeps_its_cache_ttl(self):
        """Preclose fix 7 is the trigger check and backstop only."""
        seen = {}

        def fake_fetch(repo, issue_number, project_number, **kw):
            seen.update(kw)
            return None

        with patch.object(m._item_list_cache, "fetch_issue_tier", fake_fetch), \
             patch.object(m, "resolve_repo", return_value="o/r"), \
             patch.object(m, "resolve_project_board", return_value=("owner", "3")):
            m.resolve_tier("/cwd", 1)
        self.assertEqual(seen["ttl"], m._CACHE_TTL)

    def test_lookup_failed_is_never_an_escalating_tier(self):
        self.assertNotIn(m.LOOKUP_FAILED, m.ESCALATING_TIERS)
        self.assertIs(m._TierLookupFailed(), m.LOOKUP_FAILED)

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

    def test_detached_worktree_under_the_new_root_is_a_known_target(self):
        """harmonic-forge#756: worktrees moved to ~/Harmonic_Projects/.worktrees;
        the legacy /tmp root (above) is still accepted during the migration."""
        new_root = Path.home() / "Harmonic_Projects" / ".worktrees" / "hrse2-1099-impl"
        with patch.object(m, "_run", return_value=_completed("")):
            self.assertEqual(m.resolve_issue_target(str(new_root)), (1099, "hrse"))
            for other in ("/var/tmp/hrse2-1099-impl",
                          str(Path.home() / "hrse2-1099-impl"),
                          str(new_root.parent / "nested" / "hrse2-1099-impl")):
                with self.subTest(path=other):
                    self.assertIsNone(m.resolve_issue_target(other))

    def test_false_positive_shapes_stay_unmatched(self):
        for branch in (
            "fix/lane3-worktree-staleness-warning",
            "fix/lane3-spec-derivation-filter",
            "docs/lane1-current-assignment-update",
            "docs/adr-026-federation",
            "docs/reconcile-priorities-2026-08-22",
        ):
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
        # harmonic-forge#656: settings are a fallback source now, so isolate
        # the operator's real ~/.claude/settings.json from this assertion.
        with patch.object(m.session_model, "settings_model", return_value=None), \
             patch.object(m.session_model, "recorded_model", return_value=None), \
             patch.object(m.session_model, "launch_model", return_value=None):
            self.assertTrue(m.required_tier_met({"transcript_path": str(path)}, True))

    def test_a_model_switch_after_the_last_reply_is_honored(self):
        """harmonic-forge#656 AC4: `/model opus` then an immediate resend."""
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        path = root / "t.jsonl"
        path.write_text(
            json.dumps({"type": "assistant", "message": {"model": "claude-sonnet-5"}}) + "\n"
            + json.dumps({"type": "user", "message": {"role": "user", "content":
                "<local-command-stdout>Set model to `Opus 5 (1M context)` for this "
                "session only</local-command-stdout>"}}) + "\n"
        )
        self.assertTrue(m.required_tier_met({"transcript_path": str(path)}, True))

    def test_settings_fallback_can_deny(self):
        with patch.object(m.session_model, "transcript_model", return_value=None), \
             patch.object(m.session_model, "recorded_model", return_value=None), \
             patch.object(m.session_model, "settings_model", return_value="sonnet"):
            self.assertFalse(m.required_tier_met({"transcript_path": "/nope"}, True))

    def test_codex_sol_satisfies_deep_unchanged(self):
        self.assertTrue(m.required_tier_met({"model": "gpt-5.6-sol"}, True))

    def test_codex_terra_does_not_satisfy_deep_unchanged(self):
        self.assertFalse(m.required_tier_met({"model": "gpt-5.6-terra"}, True))


class BranchReplayTests(unittest.TestCase):
    """harmonic-forge#367 Lane 1 spec, "Test oracle" section: a bidirectional
    replay over the last 100 merged head refs in each repo, fixture-frozen
    (`testdata/f367_{hrse,forge}_refs.txt`) so the assertion doesn't depend
    on live `gh` access at test time. Regenerate a fixture with:
    `gh pr list -R vitalharmony/<repo> --state merged --limit 100 --json
    headRefName -q '.[].headRefName' > tools/hooks/testdata/f367_<repo>_refs.txt`

    Two directions, both required -- "old matches still match" alone is
    exactly the invariant the second pitch-inspection round's false-positive
    class (`fix/lane3-*` -> #3, `docs/adr-026-*` -> #26) would have slipped
    past, because it only checks one direction.
    """

    OLD_BRANCH_ISSUE_RE = re.compile(r"^[\w.-]+/[a-zA-Z]?(\d+)-")
    FIXTURE_DIR = Path(__file__).parent / "testdata"

    def _refs(self, fixture_name):
        path = self.FIXTURE_DIR / f"f367_{fixture_name}_refs.txt"
        return [line for line in path.read_text().splitlines() if line.strip()]

    def _new_target(self, branch):
        with patch.object(m, "_run", return_value=_completed(branch + "\n")):
            return m.resolve_issue_target("/repo")

    def _assert_replay(self, fixture_name):
        refs = self._refs(fixture_name)
        for ref in refs:
            old_match = self.OLD_BRANCH_ISSUE_RE.match(ref)
            old_number = int(old_match.group(1)) if old_match else None
            new_target = self._new_target(ref)
            new_number = new_target[0] if new_target else None
            with self.subTest(ref=ref):
                if old_number is not None:
                    # Direction 1: every ref the old matcher resolved must
                    # still resolve, with the SAME captured number -- not
                    # just "still matches" (guards the 318 -> 321 class).
                    self.assertEqual(
                        new_number, old_number,
                        f"regression: {ref!r} captured {old_number} under the "
                        f"old matcher, {new_number} under the new one",
                    )
                elif new_number is not None:
                    # Direction 2: a ref the old matcher did NOT resolve may
                    # only newly resolve via an explicitly documented new
                    # convention -- the hint-allowlist capture group. Any
                    # other path producing a new match here is exactly the
                    # widened-character-class false-positive class the
                    # second pitch-inspection round rejected.
                    self.assertIsNotNone(
                        new_target[1],
                        f"unexplained new match: {ref!r} -> {new_number} "
                        "with no repo hint",
                    )
        return refs

    def test_hrse_replay_no_regressions_no_unexplained_matches(self):
        refs = self._assert_replay("hrse")
        self.assertEqual(len(refs), 100)

    def test_forge_replay_no_regressions_no_unexplained_matches(self):
        refs = self._assert_replay("forge")
        self.assertEqual(len(refs), 100)


class MiseTomlConsistencyTests(unittest.TestCase):
    """harmonic-forge#367 spec: assert each hardcoded HINTED_TARGETS tuple
    matches that repo's own live mise.toml -- the guard against
    `resolve_project_board()`'s own documented incident (harmonic-forge#202,
    a board renumber silently misresolving) recurring in hardcoded form.

    This test file's own repo (harmonic-forge) is always checked. The hrse
    side needs a sibling checkout this repo's CI does not have (single-repo
    checkout, see .github/workflows/ci.yml) -- it is skipped, not faked,
    when no known local hrse checkout is found, so a board renumber Marc can
    actually observe locally still fails loudly instead of the test lying
    green in an environment that can't check it.
    """

    _HRSE_CANDIDATE_PATHS = (
        Path("~/Harmonic_Projects/HRSE2").expanduser(),
        Path("~/HRSE2").expanduser(),
    )

    def _mise_toml_targets(self, repo_root: Path) -> tuple[str, str]:
        text = (repo_root / "mise.toml").read_text()
        owner = m._mise_env_value(text, "GH_PROJECT_OWNER")
        number = m._mise_env_value(text, "GH_PROJECT_NUMBER")
        repo = m._mise_env_value(text, "GH_REPO")
        self.assertIsNotNone(owner)
        self.assertIsNotNone(number)
        self.assertIsNotNone(repo)
        return repo, number

    def test_forge_hint_targets_match_live_mise_toml(self):
        repo_root = Path(__file__).resolve().parents[2]
        repo, number = self._mise_toml_targets(repo_root)
        self.assertEqual(repo, "vitalharmony/harmonic-forge")
        for hint in ("harmonic-forge", "forge", "f"):
            with self.subTest(hint=hint):
                self.assertEqual(m.HINTED_TARGETS[hint], (repo, number))

    def test_hrse_hint_targets_match_live_mise_toml(self):
        repo_root = next(
            (p for p in self._HRSE_CANDIDATE_PATHS if (p / "mise.toml").exists()),
            None,
        )
        if repo_root is None:
            self.skipTest("no local hrse checkout found to verify HINTED_TARGETS against")
        repo, number = self._mise_toml_targets(repo_root)
        self.assertEqual(repo, "vitalharmony/hrse")
        for hint in ("hrse", "h"):
            with self.subTest(hint=hint):
                self.assertEqual(m.HINTED_TARGETS[hint], (repo, number))


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


class BashWriteDetectionTests(unittest.TestCase):
    """harmonic-forge#440: `bash_command_writes_files()` must catch the exact
    shapes the incident named (redirection, `sed -i`, `tee`) while leaving
    read-only commands and heredoc-body prose alone."""

    def test_plain_redirect_with_spaces(self):
        self.assertTrue(m.bash_command_writes_files("echo hi > file.txt"))

    def test_plain_redirect_no_spaces(self):
        self.assertTrue(m.bash_command_writes_files("echo hi >file.txt"))

    def test_append_redirect(self):
        self.assertTrue(m.bash_command_writes_files("echo hi >> file.txt"))

    def test_fd_prefixed_redirect(self):
        self.assertTrue(m.bash_command_writes_files("cmd 2> err.log"))

    def test_heredoc_write_via_cat(self):
        self.assertTrue(m.bash_command_writes_files("cat > file.txt <<'EOF'\nhello\nEOF\n"))

    def test_python_heredoc_write_with_explicit_redirect(self):
        self.assertTrue(m.bash_command_writes_files(
            "python3 - <<'PY' > out.py\nprint('hi')\nPY\n"
        ))

    def test_python_heredoc_write_with_no_redirect_at_all(self):
        """The actual incident shape (hrse#1438): the interpreter writes a
        file from *inside* the (masked) script body, with no shell-level
        redirect at all -- the case the previous version of this test
        never exercised, because it always appended `> out.py`."""
        command = (
            "python3 - <<'PY'\n"
            "open('backend/app/services/foo.py', 'w').write(code)\n"
            "PY\n"
        )
        self.assertTrue(m.bash_command_writes_files(command))

    def test_other_interpreters_reading_a_heredoc_are_writes_too(self):
        for interpreter in ("node", "ruby", "perl", "bash", "sh"):
            with self.subTest(interpreter=interpreter):
                command = f"{interpreter} <<'EOF'\nsome script body\nEOF\n"
                self.assertTrue(m.bash_command_writes_files(command))

    def test_cat_reading_a_heredoc_to_stdout_is_not_a_write(self):
        """`cat` is not a script interpreter -- printing a heredoc to
        stdout, with no redirect, writes nothing."""
        self.assertFalse(m.bash_command_writes_files("cat <<'EOF'\nhello\nEOF\n"))

    def test_sed_in_place_short_flag(self):
        self.assertTrue(m.bash_command_writes_files("sed -i 's/a/b/' file.txt"))

    def test_sed_in_place_long_flag(self):
        self.assertTrue(m.bash_command_writes_files("sed --in-place 's/a/b/' file.txt"))

    def test_sed_in_place_bundled_flag(self):
        self.assertTrue(m.bash_command_writes_files("sed -ie 's/a/b/' file.txt"))

    def test_tee_writes(self):
        self.assertTrue(m.bash_command_writes_files("echo hi | tee file.txt"))

    def test_fd_duplication_is_not_a_write(self):
        """`2>&1` merges stderr into stdout -- no filesystem write."""
        self.assertFalse(m.bash_command_writes_files("cmd 2>&1"))

    def test_read_only_commands_are_not_writes(self):
        for command in ("git status", "pytest -q", "ls -la", "grep -rn foo .", "cat file.txt"):
            with self.subTest(command=command):
                self.assertFalse(m.bash_command_writes_files(command))

    def test_sed_without_in_place_is_not_a_write(self):
        self.assertFalse(m.bash_command_writes_files("sed 's/a/b/' file.txt"))

    def test_heredoc_body_prose_mentioning_redirection_is_not_a_write(self):
        """AC3: a heredoc body that merely *talks about* `>` must not trigger --
        `mask_heredoc_bodies` (shared with every other hook in this
        directory) is what keeps prose out of the parse."""
        command = (
            "cat > notes.md <<'EOF'\n"
            "redirect with > like this, or pipe to tee\n"
            "EOF\n"
        )
        # The heredoc *itself* still writes notes.md via the leading `>` --
        # assert True here, then prove the body text alone (no leading
        # write) is inert.
        self.assertTrue(m.bash_command_writes_files(command))
        body_only = "cat <<'EOF'\nredirect with > like this, or pipe to tee\nEOF\n"
        self.assertFalse(m.bash_command_writes_files(body_only))

    def test_unparseable_command_fails_open_to_no_write(self):
        self.assertFalse(m.bash_command_writes_files("echo 'unterminated"))

    def test_redirect_to_dev_null_is_not_a_write(self):
        for command in ("ls > /dev/null", "ls >/dev/null", "cmd 2>/dev/null",
                         "command -v podman >/dev/null 2>&1"):
            with self.subTest(command=command):
                self.assertFalse(m.bash_command_writes_files(command))

    def test_redirect_to_real_file_is_still_a_write(self):
        self.assertTrue(m.bash_command_writes_files("ls > /tmp/not-null.txt"))

    def test_quoted_redirect_character_is_not_a_write(self):
        for command in ("grep -rn '>' .", 'grep -rn ">" file.txt', "echo 'a>b'"):
            with self.subTest(command=command):
                self.assertFalse(m.bash_command_writes_files(command))

    def test_unquoted_redirect_adjacent_to_quoted_text_is_still_a_write(self):
        self.assertTrue(m.bash_command_writes_files("echo '>' > real_file.txt"))


class MainBashGatingTests(unittest.TestCase):
    """harmonic-forge#440: end-to-end `_main()` behavior for the `Bash`
    matcher — the regression this issue was filed for (AC7), the
    no-board-lookup-on-read-only guarantee (AC2), and the `LANE=3`
    exemption (AC4)."""

    def _run_main(self, payload: dict, env: dict | None = None) -> str:
        import io
        stdin = io.StringIO(json.dumps(payload))
        stdout = io.StringIO()
        full_env = dict(os.environ)
        full_env.pop("LANE_MODEL", None)
        full_env.pop("LANE", None)
        if env:
            full_env.update(env)
        with patch("sys.stdin", stdin), patch("sys.stdout", stdout), \
             patch.dict(os.environ, full_env, clear=True):
            try:
                m._main()
            except SystemExit:
                pass
        return stdout.getvalue()

    def _transcript(self, model: str) -> str:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        path = root / "transcript.jsonl"
        path.write_text(json.dumps({"message": {"model": model}}) + "\n")
        return str(path)

    def test_ac7_regression_bash_heredoc_write_denied_on_sonnet(self):
        """The exact incident shape: cwd on the hrse#1438 branch, tier
        `deep`, model `claude-sonnet-5`, tool `Bash` with a heredoc write."""
        payload = {
            "tool_name": "Bash",
            "cwd": "/tmp/hrse2-1438-impl",
            "transcript_path": self._transcript("claude-sonnet-5"),
            "tool_input": {"command": "cat > f.py <<'EOF'\ncode\nEOF\n"},
        }
        with patch.object(m, "_run", return_value=_completed("")), \
             patch.object(m, "resolve_tier", return_value="deep"):
            out = self._run_main(payload)
        self.assertIn('"permissionDecision": "deny"', out)

    def test_ac7_regression_lane3_allows_the_same_payload(self):
        payload = {
            "tool_name": "Bash",
            "cwd": "/tmp/hrse2-1438-impl",
            "transcript_path": self._transcript("claude-sonnet-5"),
            "tool_input": {"command": "cat > f.py <<'EOF'\ncode\nEOF\n"},
        }
        with patch.object(m, "resolve_tier") as fake_resolve_tier:
            out = self._run_main(payload, env={"LANE": "3"})
            fake_resolve_tier.assert_not_called()
        self.assertEqual(out, "")

    def test_ac2_read_only_bash_never_calls_resolve_tier(self):
        payload = {
            "tool_name": "Bash",
            "cwd": "/tmp/hrse2-1438-impl",
            "tool_input": {"command": "git status"},
        }
        with patch.object(m, "resolve_tier") as fake_resolve_tier:
            out = self._run_main(payload)
            fake_resolve_tier.assert_not_called()
        self.assertEqual(out, "")

    def test_ac1_bash_write_denied_matches_edit_denial_shape(self):
        payload = {
            "tool_name": "Bash",
            "cwd": "/tmp/hrse2-1438-impl",
            "transcript_path": self._transcript("claude-sonnet-5"),
            "tool_input": {"command": "sed -i 's/a/b/' f.py"},
        }
        with patch.object(m, "_run", return_value=_completed("")), \
             patch.object(m, "resolve_tier", return_value="deep"):
            out = self._run_main(payload)
        self.assertIn("/model opus", out)

    def test_ac5_edit_write_behavior_unchanged_without_lane(self):
        payload = {
            "tool_name": "Edit",
            "cwd": "/tmp/hrse2-1438-impl",
            "transcript_path": self._transcript("claude-sonnet-5"),
        }
        with patch.object(m, "_run", return_value=_completed("")), \
             patch.object(m, "resolve_tier", return_value="deep"):
            out = self._run_main(payload)
        self.assertIn('"permissionDecision": "deny"', out)

    def test_656_lookup_failure_denies_edit_on_sonnet(self):
        payload = {
            "tool_name": "Edit",
            "cwd": "/tmp/hrse2-1438-impl",
            "transcript_path": self._transcript("claude-sonnet-5"),
        }
        with patch.object(m, "_run", return_value=_completed("")), \
             patch.object(m, "resolve_tier", return_value=m.LOOKUP_FAILED):
            out = self._run_main(payload)
        self.assertIn('"permissionDecision": "deny"', out)
        self.assertIn("Tier lookup failed for issue #1438", out)
        self.assertNotIn("LANE_MODEL", out, "the lookup-failed deny names no override")

    def test_656_lookup_failure_allows_edit_on_opus(self):
        payload = {
            "tool_name": "Edit",
            "cwd": "/tmp/hrse2-1438-impl",
            "transcript_path": self._transcript("claude-opus-5"),
        }
        with patch.object(m, "_run", return_value=_completed("")), \
             patch.object(m, "resolve_tier", return_value=m.LOOKUP_FAILED):
            out = self._run_main(payload)
        self.assertEqual(out, "")

    def test_656_no_branch_issue_stays_fail_open(self):
        payload = {
            "tool_name": "Edit",
            "cwd": "/nonexistent/no-branch",
            "transcript_path": self._transcript("claude-sonnet-5"),
        }
        with patch.object(m, "resolve_issue_target", return_value=None), \
             patch.object(m, "resolve_tier") as fake_resolve_tier:
            out = self._run_main(payload)
            fake_resolve_tier.assert_not_called()
        self.assertEqual(out, "")

    def test_656_unset_tier_stays_fail_open(self):
        payload = {
            "tool_name": "Edit",
            "cwd": "/tmp/hrse2-1438-impl",
            "transcript_path": self._transcript("claude-sonnet-5"),
        }
        with patch.object(m, "_run", return_value=_completed("")), \
             patch.object(m, "resolve_tier", return_value=None):
            out = self._run_main(payload)
        self.assertEqual(out, "")

    def test_ac4_lane3_allows_edit_write_too(self):
        payload = {
            "tool_name": "Edit",
            "cwd": "/tmp/hrse2-1438-impl",
            "transcript_path": self._transcript("claude-sonnet-5"),
        }
        with patch.object(m, "resolve_tier") as fake_resolve_tier:
            out = self._run_main(payload, env={"LANE": "3"})
            fake_resolve_tier.assert_not_called()
        self.assertEqual(out, "")



class WriteTargetResolutionTests(unittest.TestCase):
    """harmonic-forge#630 -- the gate resolves from the write target, not
    the session's own cwd."""

    def _run_main(self, payload: dict, env: dict | None = None) -> str:
        return MainBashGatingTests._run_main(self, payload, env)

    def _transcript(self, model: str) -> str:
        return MainBashGatingTests._transcript(self, model)

    def test_ac1_edit_resolved_from_file_path_not_cwd(self):
        """The session sits on `main` (no issue); the file it edits is in a
        `deep`-tier worktree. AC1: resolved from the write target."""
        payload = {
            "tool_name": "Edit",
            "cwd": "/home/mmangus/Harmonic_Projects/HRSE2",
            "transcript_path": self._transcript("claude-sonnet-5"),
            "tool_input": {"file_path": "/tmp/hrse2-1438-impl/backend/app/x.py"},
        }
        with patch.object(m.os.path, "isdir", side_effect=lambda d: d == "/tmp/hrse2-1438-impl"), \
             patch.object(m, "resolve_tier", return_value="deep") as fake_tier:
            out = self._run_main(payload)
        self.assertIn('"permissionDecision": "deny"', out)
        # resolved against the FILE's worktree dir, never the session cwd
        self.assertEqual(fake_tier.call_args[0][0], "/tmp/hrse2-1438-impl")

    def test_ac2_main_checkout_no_longer_bypasses_a_deep_worktree_write(self):
        """The exact bypass the issue was filed for: a session on `main`
        (resolves to no issue) writing into a `deep`-tier worktree by
        absolute path must still be gated."""
        payload = {
            "tool_name": "Write",
            "cwd": "/home/mmangus/Harmonic_Projects/HRSE2",
            "transcript_path": self._transcript("claude-sonnet-5"),
            "tool_input": {"file_path": "/tmp/hrse2-1438-impl/new_file.py"},
        }
        with patch.object(m.os.path, "isdir", side_effect=lambda d: d == "/tmp/hrse2-1438-impl"), \
             patch.object(m, "resolve_tier", return_value="deep"):
            out = self._run_main(payload)
        self.assertIn('"permissionDecision": "deny"', out)

    def test_ac1_bash_still_falls_back_to_cwd_unchanged(self):
        """Bash has no extractable write target (`bash_command_writes_files`
        proves a write happens, never where) -- AC1's stated fallback case."""
        payload = {
            "tool_name": "Bash",
            "cwd": "/tmp/hrse2-1438-impl",
            "transcript_path": self._transcript("claude-sonnet-5"),
            "tool_input": {"command": "echo x > f.py"},
        }
        with patch.object(m, "_run", return_value=_completed("")),              patch.object(m, "resolve_tier", return_value="deep") as fake_tier:
            out = self._run_main(payload)
        self.assertIn('"permissionDecision": "deny"', out)
        self.assertEqual(fake_tier.call_args[0][0], "/tmp/hrse2-1438-impl")

    def test_ac3_two_targets_in_one_apply_patch_gates_on_the_worse_one(self):
        """AC3: a single apply_patch touching two different issues' worktrees
        does not silently pick one -- the `deep` target must still deny even
        though it is listed second."""
        patch_body = (
            "*** Begin Patch\n"
            "*** Update File: /tmp/hrse2-100-impl/a.py\n"
            "*** Update File: /tmp/hrse2-200-impl/b.py\n"
            "*** End Patch\n"
        )
        payload = {
            "tool_name": "apply_patch",
            "model": "gpt-5.6-mini",  # a real, non-high Codex model
            "cwd": "/some/other/dir",
            "tool_input": {"command": patch_body},
        }

        def fake_isdir(d):
            return d in ("/tmp/hrse2-100-impl", "/tmp/hrse2-200-impl")

        def fake_resolve_tier(d, issue_number, repo_hint):
            return "fast" if issue_number == 100 else "deep"

        with patch.object(m.os.path, "isdir", side_effect=fake_isdir), \
             patch.object(m, "resolve_tier", side_effect=fake_resolve_tier):
            out = self._run_main(payload)
        self.assertIn('"permissionDecision": "deny"', out)
        self.assertIn("#200", out)

    def test_ac3_two_targets_neither_deep_allows(self):
        patch_body = (
            "*** Begin Patch\n"
            "*** Update File: /tmp/hrse2-100-impl/a.py\n"
            "*** Update File: /tmp/hrse2-200-impl/b.py\n"
            "*** End Patch\n"
        )
        payload = {
            "tool_name": "apply_patch",
            "model": "gpt-5.6-mini",
            "cwd": "/some/other/dir",
            "tool_input": {"command": patch_body},
        }

        def fake_isdir(d):
            return d in ("/tmp/hrse2-100-impl", "/tmp/hrse2-200-impl")

        with patch.object(m.os.path, "isdir", side_effect=fake_isdir), \
             patch.object(m, "resolve_tier", return_value="fast"):
            out = self._run_main(payload)
        self.assertEqual(out, "")

    def test_ac4_required_tier_met_fail_open_logs_to_stderr(self):
        import io
        stderr = io.StringIO()
        payload = {"transcript_path": "/does/not/exist.jsonl", "cwd": "/tmp"}
        with patch.object(m.session_model, "current_model", return_value=None),              patch("sys.stderr", stderr):
            result = m.required_tier_met(payload, high_required=True)
        self.assertTrue(result)
        self.assertIn("failing open", stderr.getvalue())

    def test_ac5_seam_write_target_deep_session_cwd_no_issue(self):
        """The exact adversarial case AC5 names: the write target resolves to
        a `deep` issue while the session's own cwd resolves to NO issue at
        all. Mutation-checked below."""
        payload = {
            "tool_name": "Edit",
            "cwd": "/home/mmangus/Harmonic_Projects/HRSE2",  # main, no issue
            "transcript_path": self._transcript("claude-sonnet-5"),
            "tool_input": {"file_path": "/tmp/hrse2-9999-impl/x.py"},
        }
        with patch.object(m.os.path, "isdir", side_effect=lambda d: d == "/tmp/hrse2-9999-impl"), \
             patch.object(m, "resolve_tier", return_value="deep"):
            out = self._run_main(payload)
        self.assertIn('"permissionDecision": "deny"', out)
        self.assertIn("#9999", out)

    def test_ac5_mutation_reverting_to_cwd_only_fails_the_seam_test(self):
        """Confirms test_ac5 above actually exercises the fix: with
        `write_target_dirs` forced to report no target (the pre-fix
        behavior), the same payload must be ALLOWED -- proving the seam
        test would have failed against the original bug."""
        payload = {
            "tool_name": "Edit",
            "cwd": "/home/mmangus/Harmonic_Projects/HRSE2",
            "transcript_path": self._transcript("claude-sonnet-5"),
            "tool_input": {"file_path": "/tmp/hrse2-9999-impl/x.py"},
        }
        with patch.object(m, "write_target_dirs", return_value=None), \
             patch.object(m, "resolve_issue_target", return_value=None):
            out = self._run_main(payload)
        self.assertEqual(out, "", "pre-fix behavior (cwd-only) allows this "
                          "write -- confirms the AC5 test is load-bearing")


class PrecloseFindingRegressionTests(unittest.TestCase):
    """harmonic-forge#630 preclose-inspection findings 1-3 (2026-09-16),
    each reproduced against real git state, not mocked resolution --
    the panel's own point was that the mocked tests above did not exercise
    a nested path or a relative apply_patch target."""

    def setUp(self):
        self.worktree = Path(tempfile.mkdtemp(prefix="hrse2-888888-impl-",
                                              dir="/tmp"))
        # WORKTREE_ISSUE_RE needs the EXACT name; tempfile's suffix breaks
        # that, so build the real path by hand and clean it up ourselves.
        shutil.rmtree(self.worktree, ignore_errors=True)
        self.worktree = Path("/tmp/hrse2-888888-impl")
        (self.worktree / "backend" / "app").mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.worktree, ignore_errors=True)
        subprocess.run(["git", "init", "-q"], cwd=self.worktree, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=self.worktree, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=self.worktree, check=True)
        (self.worktree / "README").write_text("x")
        subprocess.run(["git", "add", "-A"], cwd=self.worktree, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=self.worktree, check=True)
        subprocess.run(["git", "checkout", "-q", "--detach"], cwd=self.worktree, check=True)

    def test_finding1_nested_path_in_detached_head_worktree_still_resolves(self):
        """A file nested under a worktree whose branch is detached (a
        conflicted rebase, or any non-conforming branch name) must still
        resolve via the worktree-root fallback -- not silently fall
        through because the nearest existing directory is a subdirectory."""
        nested = str(self.worktree / "backend" / "app" / "x.py")
        dirs = m.write_target_dirs({"tool_name": "Edit",
                                    "tool_input": {"file_path": nested}})
        self.assertEqual(dirs, [str(self.worktree)])
        self.assertEqual(m.resolve_issue_target(dirs[0]), (888888, "hrse"))

    def test_finding1_end_to_end_deny_survives_detached_head(self):
        payload = {
            "tool_name": "Edit",
            "cwd": "/home/mmangus/Harmonic_Projects/HRSE2",
            "transcript_path": None,
            "tool_input": {"file_path": str(self.worktree / "backend" / "app" / "x.py")},
        }
        import io
        stdin = io.StringIO(json.dumps(payload))
        stdout = io.StringIO()
        with patch("sys.stdin", stdin), patch("sys.stdout", stdout), \
             patch.dict(os.environ, {k: v for k, v in os.environ.items()
                                     if k not in ("LANE_MODEL", "LANE")}, clear=True), \
             patch.object(m, "resolve_tier", return_value="deep"), \
             patch.object(m.session_model, "current_model", return_value="claude-sonnet-5"):
            try:
                m._main()
            except SystemExit:
                pass
        self.assertIn('"permissionDecision": "deny"', stdout.getvalue())
        self.assertIn("#888888", stdout.getvalue())

    def test_finding2_relative_apply_patch_target_resolves_against_payload_cwd(self):
        """A repo-relative apply_patch target must resolve against
        `payload["cwd"]`, never the hook process's own unrelated cwd."""
        body = "*** Begin Patch\n*** Update File: backend/app/x.py\n*** End Patch\n"
        payload = {
            "tool_name": "apply_patch",
            "cwd": str(self.worktree),
            "tool_input": {"command": body},
        }
        self.assertEqual(m.write_target_dirs(payload), [str(self.worktree)])

    def test_finding2_relative_target_ignores_hook_process_cwd(self):
        """Regression guard: resolving from the hook's OWN cwd (not the
        payload's) would resolve to whatever repo this test happens to run
        in -- assert it does NOT, by running from a directory that is not
        the target worktree at all."""
        body = "*** Begin Patch\n*** Update File: backend/app/x.py\n*** End Patch\n"
        payload = {
            "tool_name": "apply_patch",
            "cwd": str(self.worktree),
            "tool_input": {"command": body},
        }
        real_cwd = os.getcwd()
        try:
            os.chdir(tempfile.gettempdir())
            dirs = m.write_target_dirs(payload)
        finally:
            os.chdir(real_cwd)
        self.assertEqual(dirs, [str(self.worktree)])

    def test_finding3_target_resolving_to_no_issue_logs_to_stderr(self):
        """AC4, extended to the resolution path this issue itself added:
        a write target that resolves to no tracked issue at all must not
        allow in total silence."""
        payload = {
            "tool_name": "Edit",
            "cwd": "/does/not/matter",
            "tool_input": {"file_path": "/tmp/definitely-not-a-worktree/x.py"},
        }
        import io
        stdin = io.StringIO(json.dumps(payload))
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("sys.stdin", stdin), patch("sys.stdout", stdout), \
             patch("sys.stderr", stderr), \
             patch.dict(os.environ, {k: v for k, v in os.environ.items()
                                     if k not in ("LANE_MODEL", "LANE")}, clear=True):
            try:
                m._main()
            except SystemExit:
                pass
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("no tracked issue resolved", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
