#!/usr/bin/env python3
"""Unit tests for tier_model_trigger_check.py (harmonic-forge#656 AC1-AC3,
AC7). Tier lookups and the cwd repo are mocked; no gh call is made.
Run: python3 tools/hooks/test_tier_model_trigger_check.py"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import model_tier_gate  # noqa: E402
import tier_model_trigger_check as t  # noqa: E402

HRSE = "vitalharmony/hrse"
FORGE = "vitalharmony/harmonic-forge"


def refs(prompt, cwd_repo=HRSE):
    with patch.object(model_tier_gate, "resolve_repo", return_value=cwd_repo):
        return t.collect_refs(prompt, "/cwd")


class FakeTiers:
    def __init__(self, tiers):
        self.tiers = tiers
        self.calls = []

    def __call__(self, repo, number):
        self.calls.append((repo, number))
        value = self.tiers.get((repo, number))
        if value == "FAIL":
            return model_tier_gate.LOOKUP_FAILED, "HTTP 403: API rate limit exceeded"
        return value, None


def run(prompt, model, tiers, lane="2", extra_env=None, cwd_repo=HRSE):
    env = {"LANE": lane} if lane else {}
    env.update(extra_env or {})
    fake = FakeTiers(tiers)
    with patch.object(model_tier_gate, "resolve_repo", return_value=cwd_repo):
        out = t.run({"prompt": prompt, "cwd": "/cwd"}, env=env, lookup=fake, model=model)
    return out, fake


class ParserTests(unittest.TestCase):
    def test_each_verb_with_prefixed_ref(self):
        for verb in ("Implement", "Plan", "Fix", "Blueprint", "Test", "Verify",
                     "Approve and execute", "Run Lane 2 gate", "Lane 2 execute",
                     "Replan", "Reimplement"):
            with self.subTest(verb=verb):
                self.assertEqual(refs(f"{verb} H1830"), [(HRSE, 1830)])

    def test_each_repo_prefix(self):
        self.assertEqual(refs("Implement F656"), [(FORGE, 656)])
        self.assertEqual(refs("Plan I344"), [("vitalharmony/cymagraph-infra", 344)])
        self.assertEqual(refs("Plan O12"), [("vitalharmony/openclaw-projects", 12)])

    def test_lowercase_prefix_and_verb(self):
        self.assertEqual(refs("plan h1430"), [(HRSE, 1430)])

    def test_then_chain_reads_both(self):
        """Added case (b)."""
        self.assertEqual(refs("Plan H1437 then H1443"), [(HRSE, 1437), (HRSE, 1443)])

    def test_bare_hash_resolves_against_cwd_repo(self):
        """Added case (c): `Plan #794` in an HRSE2 cwd is hrse#794."""
        self.assertEqual(refs("Plan #794"), [(HRSE, 794)])
        self.assertEqual(refs("Plan #794", cwd_repo=FORGE), [(FORGE, 794)])

    def test_verb_and_bare_number_chain(self):
        self.assertEqual(refs("plan 1169"), [(HRSE, 1169)])
        self.assertEqual(refs("Implement 1433, 1434 then 1435"),
                         [(HRSE, 1433), (HRSE, 1434), (HRSE, 1435)])

    def test_owner_repo_ref_and_belt_notification(self):
        """Added case (a)'s prompt shape."""
        self.assertEqual(refs("vitalharmony/hrse#1830 queued-for-l2 kind=handoff"),
                         [(HRSE, 1830)])

    def test_issue_url(self):
        self.assertEqual(refs("see https://github.com/vitalharmony/harmonic-forge/issues/650"),
                         [(FORGE, 650)])

    def test_duplicates_are_collapsed(self):
        self.assertEqual(refs("Plan H1830, vitalharmony/hrse#1830 and #1830"), [(HRSE, 1830)])

    def test_fenced_code_is_ignored(self):
        self.assertEqual(refs("```\nImplement H1830 #12\n```\nthanks"), [])

    def test_bare_number_without_verb_is_not_a_ref(self):
        self.assertEqual(refs("wait 1830 seconds"), [])

    def test_refs_are_in_prompt_order_not_kind_order(self):
        """Preclose fix 1: a bare `#1830` named first used to sort behind every
        prefixed `H` ref, and past the read cap."""
        self.assertEqual(
            refs("Plan #1830 -- follow-up to H1801, H1806, H1807, H1811, H1813 and H1771"),
            [(HRSE, 1830), (HRSE, 1801), (HRSE, 1806), (HRSE, 1807), (HRSE, 1811),
             (HRSE, 1813), (HRSE, 1771)])

    def test_mixed_order_kinds_interleave(self):
        self.assertEqual(
            refs("see vitalharmony/harmonic-forge#650, then Implement H1830 and #12"),
            [(FORGE, 650), (HRSE, 1830), (HRSE, 12)])

    def test_owner_repo_and_url_are_lowercased(self):
        """Preclose fix 2: `VitalHarmony/HRSE#1830` is hrse#1830."""
        self.assertEqual(refs("Implement VitalHarmony/HRSE#1830"), [(HRSE, 1830)])
        self.assertEqual(refs("https://github.com/VitalHarmony/Harmonic-Forge/issues/650"),
                         [(FORGE, 650)])

    def test_no_cwd_repo_drops_bare_refs_only(self):
        self.assertEqual(refs("Plan #794 and H1830", cwd_repo=None), [(HRSE, 1830)])


class DecisionTests(unittest.TestCase):
    def test_deep_on_sonnet_blocks(self):
        """TC1 offline."""
        out, _ = run("Plan H1830", "claude-sonnet-5", {(HRSE, 1830): "deep"})
        self.assertEqual(out["decision"], "block")
        self.assertIn("vitalharmony/hrse#1830 is Tier deep", out["reason"])
        self.assertIn("claude-sonnet-5", out["reason"])
        self.assertIn("/model opus", out["reason"])

    def test_deep_on_opus_display_name_passes(self):
        """TC2 offline: the model string after `/model opus` is a display name."""
        out, _ = run("Plan H1830", "Opus 5 (1M context)", {(HRSE, 1830): "deep"})
        self.assertIsNone(out)

    def test_deep_on_fable_passes(self):
        out, _ = run("Plan H1830", "claude-fable-5", {(HRSE, 1830): "deep"})
        self.assertIsNone(out)

    def test_fast_on_opus_suggests_sonnet(self):
        """TC3 offline / AC3."""
        out, _ = run("Implement H1824", "claude-opus-5", {(HRSE, 1824): "fast"})
        self.assertNotIn("decision", out)
        self.assertEqual(out["systemMessage"],
                         "vitalharmony/hrse#1824 is Tier fast; /model sonnet is sufficient.")

    def test_fast_on_sonnet_is_silent(self):
        out, _ = run("Implement H1824", "claude-sonnet-5", {(HRSE, 1824): "fast"})
        self.assertIsNone(out)

    def test_no_sonnet_suggestion_when_another_ref_is_deep(self):
        out, _ = run("Plan H1830 then H1824", "claude-opus-5",
                     {(HRSE, 1830): "deep", (HRSE, 1824): "standard"})
        self.assertIsNone(out)

    def test_unreadable_tier_on_sonnet_blocks_with_reason(self):
        """TC4 offline / AC2."""
        out, _ = run("Implement H1810", "claude-sonnet-5", {(HRSE, 1810): "FAIL"})
        self.assertEqual(out["decision"], "block")
        self.assertIn("could not be read (HTTP 403: API rate limit exceeded)", out["reason"])
        self.assertIn("LANE_MODEL", out["reason"])
        self.assertIn("/model opus", out["reason"])

    def test_unreadable_tier_on_opus_proceeds_with_note(self):
        """Added case (d)."""
        out, _ = run("Implement H1810", "claude-opus-5", {(HRSE, 1810): "FAIL"})
        self.assertNotIn("decision", out)
        self.assertIn("Tier for vitalharmony/hrse#1810 could not be read", out["systemMessage"])

    def test_unresolved_model_is_not_high(self):
        out, _ = run("Plan H1830", None, {(HRSE, 1830): "deep"})
        self.assertEqual(out["decision"], "block")
        self.assertIn("an unresolved model", out["reason"])

    def test_bare_queue_line_typed_by_operator_still_blocks(self):
        """Added case (a), as operator-typed text: still a block."""
        out, _ = run("vitalharmony/hrse#1830 queued-for-l2 kind=handoff",
                     "claude-sonnet-5", {(HRSE, 1830): "deep"})
        self.assertEqual(out["decision"], "block")

    def test_task_notification_warns_instead_of_blocking(self):
        """Operator ruling 2026-09-14: a belt notification lists the whole
        queue; blocking it erased every belt tick of a Sonnet lane."""
        prompt = ("<task-notification>\n<task-id>b1</task-id>\n"
                  "<summary>Monitor event: \"Lane 2 belt\"</summary>\n"
                  "<event>vitalharmony/hrse#1830 queued-for-l2 kind=handoff</event>\n"
                  "</task-notification>")
        out, _ = run(prompt, "claude-sonnet-5", {(HRSE, 1830): "deep"})
        self.assertNotIn("decision", out)
        self.assertIn("#1830 is Tier deep", out["systemMessage"])
        self.assertIn("Do NOT start", out["hookSpecificOutput"]["additionalContext"])

    def test_task_notification_past_read_cap_warns_not_blocks(self):
        events = "\n".join(f"vitalharmony/hrse#{n} queued-for-l2 kind=handoff"
                           for n in range(1100, 1111))
        prompt = f"<task-notification>\n<event>{events}</event>\n</task-notification>"
        out, _ = run(prompt, "claude-sonnet-5", {})
        self.assertNotIn("decision", out)
        self.assertIn("not checked", out["systemMessage"])

    def test_typed_prompt_mentioning_task_notification_midway_still_blocks(self):
        out, _ = run("Plan H1830 <task-notification>", "claude-sonnet-5",
                     {(HRSE, 1830): "deep"})
        self.assertEqual(out["decision"], "block")

    def test_then_chain_checks_both_and_blocks_on_second(self):
        out, fake = run("Plan H1437 then H1443", "claude-sonnet-5",
                        {(HRSE, 1437): "standard", (HRSE, 1443): "deep"})
        self.assertEqual(fake.calls, [(HRSE, 1437), (HRSE, 1443)])
        self.assertIn("#1443 is Tier deep", out["reason"])

    def test_read_cap_blocks_a_non_high_model(self):
        """Preclose fix 1: a ref past the cap is unchecked, so on a non-high
        model it blocks rather than going through silently."""
        prompt = "Plan " + ", ".join(f"H{n}" for n in range(1100, 1108))
        out, fake = run(prompt, "claude-sonnet-5", {})
        self.assertEqual(len(fake.calls), t._MAX_TIER_READS)
        self.assertEqual(out["decision"], "block")
        self.assertIn("Too many issue references to check -- resend naming fewer issues",
                      out["reason"])
        self.assertIn("vitalharmony/hrse#1106, vitalharmony/hrse#1107", out["reason"])

    def test_read_cap_on_a_high_model_lists_unchecked_without_blocking(self):
        prompt = "Plan " + ", ".join(f"H{n}" for n in range(1100, 1108))
        out, fake = run(prompt, "claude-opus-5", {})
        self.assertEqual(len(fake.calls), t._MAX_TIER_READS)
        self.assertNotIn("decision", out)
        self.assertIn("vitalharmony/hrse#1106, vitalharmony/hrse#1107", out["systemMessage"])

    def test_deep_issue_named_first_among_seven_is_checked(self):
        """The preclose scenario, verbatim."""
        out, fake = run("Plan #1830 -- follow-up to H1801, H1806, H1807, H1811, H1813 and H1771",
                        "claude-sonnet-5", {(HRSE, 1830): "deep"})
        self.assertEqual(fake.calls[0], (HRSE, 1830))
        self.assertEqual(out["decision"], "block")
        self.assertIn("vitalharmony/hrse#1830 is Tier deep", out["reason"])

    def test_mixed_case_owner_repo_deep_blocks(self):
        """Preclose fix 2 through the real board lookup: the manifest's lowercase
        key must match a capitalized ref."""
        with patch.object(model_tier_gate._item_list_cache, "fetch_issue_tier",
                          return_value="deep"), \
             patch.object(t, "_boards", return_value={HRSE: "1"}):
            out = t.run({"prompt": "Implement VitalHarmony/HRSE#1830", "cwd": "/cwd"},
                        env={"LANE": "2"}, model="claude-sonnet-5")
        self.assertEqual(out["decision"], "block")

    def test_unknown_repo_on_sonnet_is_a_note_not_silence(self):
        """Preclose fix 2: a repo with no board is reported, never read as no Tier."""
        with patch.object(t, "_boards", return_value={HRSE: "1"}):
            out = t.run({"prompt": "Implement someone/else#5", "cwd": "/cwd"},
                        env={"LANE": "2"}, model="claude-sonnet-5")
        self.assertNotIn("decision", out)
        self.assertIn("no project board is known for someone/else", out["systemMessage"])

    def test_no_tier_set_is_silent(self):
        out, _ = run("Plan H1830", "claude-sonnet-5", {})
        self.assertIsNone(out)


class ScopeTests(unittest.TestCase):
    def test_lane3_is_exempt(self):
        """TC5 offline / AC7."""
        out, fake = run("Test H1810", "claude-sonnet-5", {(HRSE, 1810): "deep"}, lane="3")
        self.assertIsNone(out)
        self.assertEqual(fake.calls, [])

    def test_no_lane_is_inactive(self):
        out, fake = run("Plan H1830", "claude-sonnet-5", {(HRSE, 1830): "deep"}, lane=None)
        self.assertIsNone(out)
        self.assertEqual(fake.calls, [])

    def test_lane1_is_active(self):
        out, _ = run("Plan H1830", "claude-sonnet-5", {(HRSE, 1830): "deep"}, lane="1")
        self.assertEqual(out["decision"], "block")

    def test_lane_model_override_allows_without_reading(self):
        out, fake = run("Plan H1830", "claude-sonnet-5", {(HRSE, 1830): "deep"},
                        extra_env={"LANE_MODEL": "opus"})
        self.assertNotIn("decision", out)
        self.assertIn("LANE_MODEL=opus", out["systemMessage"])
        self.assertEqual(fake.calls, [])

    def test_prompt_without_refs_reads_nothing(self):
        out, fake = run("continue", "claude-sonnet-5", {})
        self.assertIsNone(out)
        self.assertEqual(fake.calls, [])


class RealLookupPathTests(unittest.TestCase):
    def test_board_reads_are_fresh_and_use_the_manifest_board(self):
        """Preclose fix 7: `ttl=0`, never the edit gate's 120 s cache."""
        seen = {}

        def fake_fetch(repo, issue_number, project_number, **kw):
            seen.update(repo=repo, issue=issue_number, project=project_number, kw=kw)
            return "deep"

        with patch.object(model_tier_gate._item_list_cache, "fetch_issue_tier", fake_fetch):
            tier, error = t.lookup_tier("vitalharmony/cymagraph-infra", 344, t._boards())
        self.assertEqual((tier, error), ("deep", None))
        self.assertEqual(seen["project"], "1")
        self.assertEqual(seen["kw"]["ttl"], 0)

    def test_a_raised_tier_is_seen_despite_a_warm_gate_cache(self):
        """Preclose fix 7 end to end: a cached "no Tier" written by the edit
        gate must not hide a Tier raised since."""
        cache_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, cache_dir, ignore_errors=True)

        def payload(tier):
            node = {"project": {"number": 1}, "value": {"name": tier} if tier else None}
            return subprocess.CompletedProcess([], 0, json.dumps({"data": {"repository": {
                "issue": {"projectItems": {"nodes": [node]}}}}}), "")

        with patch.object(model_tier_gate, "_CACHE_DIR", cache_dir):
            self.assertEqual(model_tier_gate.read_tier(HRSE, 1830, "1",
                                                       run=lambda cmd: payload(None)), (None, None))
            with patch.object(model_tier_gate, "timed_run", lambda cmd, timeout=None: payload("deep")):
                self.assertEqual(t.lookup_tier(HRSE, 1830, {HRSE: "1"}), ("deep", None))

    def test_timed_run_passes_a_timeout_to_subprocess(self):
        """Preclose fix 9: the old test raised TimeoutExpired itself and passed
        with the timeout deleted."""
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as sp:
            t._timed_run(["gh", "api", "graphql"])
        self.assertEqual(sp.call_args.kwargs.get("timeout"), t._READ_TIMEOUT_SECONDS)

    def test_timeout_is_a_failed_read_not_an_allow(self):
        import subprocess

        def slow(*a, **k):
            raise subprocess.TimeoutExpired(cmd="gh", timeout=4)

        with patch.object(model_tier_gate._item_list_cache, "fetch_issue_tier", slow):
            tier, error = t.lookup_tier(HRSE, 1810, {HRSE: "1"})
        self.assertIs(tier, model_tier_gate.LOOKUP_FAILED)
        self.assertIn("timed out", error)

    def test_unknown_repo_is_not_read(self):
        with patch.object(model_tier_gate._item_list_cache, "fetch_issue_tier") as fetch:
            self.assertEqual(t.lookup_tier("someone/else", 1, t._boards()), (t.NO_BOARD, None))
        fetch.assert_not_called()

    def _run_real(self, prompt, error, model="claude-sonnet-5"):
        def boom(*a, **k):
            raise model_tier_gate._item_list_cache.GhItemListError(error)

        with patch.object(model_tier_gate._item_list_cache, "fetch_issue_tier", boom), \
             patch.object(t, "_boards", return_value={HRSE: "1"}), \
             patch.object(model_tier_gate, "resolve_repo", return_value=HRSE):
            return t.run({"prompt": prompt, "cwd": "/cwd"}, env={"LANE": "2"}, model=model)

    def test_pr_number_is_skipped_with_a_note_not_blocked(self):
        """Preclose fix 3: "Implement H1824, see PR #1833" blocked for good."""
        out = self._run_real(
            "see PR #1833",
            "GraphQL: Could not resolve to an Issue with the number of 1833. (repository.issue)")
        self.assertNotIn("decision", out)
        self.assertIn("vitalharmony/hrse#1833 is not an issue", out["systemMessage"])

    def test_not_found_type_is_not_an_issue(self):
        out = self._run_real("see #1833", "NOT_FOUND: no issue")
        self.assertNotIn("decision", out)

    def test_a_403_is_still_a_failed_read(self):
        out = self._run_real("Implement #1833", "HTTP 403: API rate limit exceeded")
        self.assertEqual(out["decision"], "block")
        self.assertIn("could not be read (HTTP 403", out["reason"])


class TranscriptModelTests(unittest.TestCase):
    """Preclose fix 8: every DecisionTests case passes `model` straight into
    `run`, so `session_model.current_model` in `run()` was untested."""

    def _run(self, model_id):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        transcript = root / "t.jsonl"
        transcript.write_text(json.dumps({
            "isSidechain": False, "type": "attachment",
            "attachment": {"type": "model", "identity": {"modelId": model_id}},
        }) + "\n")
        fake = FakeTiers({(HRSE, 1830): "deep"})
        with patch.object(model_tier_gate, "resolve_repo", return_value=HRSE):
            return t.run({"prompt": "Plan H1830", "cwd": str(root),
                          "transcript_path": str(transcript)},
                         env={"LANE": "2"}, lookup=fake)

    def test_sonnet_transcript_blocks_on_deep(self):
        out = self._run("claude-sonnet-5")
        self.assertEqual(out["decision"], "block")
        self.assertIn("claude-sonnet-5", out["reason"])

    def test_opus_transcript_allows_deep(self):
        self.assertIsNone(self._run("claude-opus-5[1m]"))


class MainTests(unittest.TestCase):
    def test_internal_error_fails_open_with_stderr(self):
        stdin = io.StringIO(json.dumps({"prompt": "Plan H1830"}))
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("sys.stdin", stdin), patch("sys.stdout", stdout), \
             patch("sys.stderr", stderr), patch.dict(os.environ, {"LANE": "2"}), \
             patch.object(t, "collect_refs", side_effect=RuntimeError("boom")):
            t.main()
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("internal error", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
