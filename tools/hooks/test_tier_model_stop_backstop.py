#!/usr/bin/env python3
"""Unit tests for tier_model_stop_backstop.py (harmonic-forge#656 AC5).
Synthetic transcripts; Tier lookups and the cwd repo are mocked.
Run: python3 tools/hooks/test_tier_model_stop_backstop.py"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import model_tier_gate  # noqa: E402
import tier_model_stop_backstop as b  # noqa: E402

HRSE = "vitalharmony/hrse"
FORGE = "vitalharmony/harmonic-forge"


def targets(command, cwd_repo=HRSE):
    return b.posted_targets(command, lambda: cwd_repo)


class PosterDetectionTests(unittest.TestCase):
    def test_l1_post_defaults_to_hrse(self):
        self.assertEqual(targets("python3 scripts/l1_post.py --issue 1830 --kind handoff "
                                 "--sha abc --branch main --file /tmp/h.md", cwd_repo=FORGE),
                         [(HRSE, 1830)])

    def test_l2_post_post_only(self):
        cmd = ("python3 ~/harmonic-forge/tools/gh/l2_post.py post --kind plan "
               "--issue 650 --repo vitalharmony/harmonic-forge --narrative-file n.md")
        self.assertEqual(targets(cmd), [(FORGE, 650)])
        self.assertEqual(targets("python3 tools/gh/l2_post.py snapshot --repo x/y --issue 1"), [])

    def test_post_comment_with_equals_flags(self):
        self.assertEqual(targets("python3 tools/gh/post_comment.py --repo=vitalharmony/hrse "
                                 "--issue=1810 --body hi"), [(HRSE, 1810)])

    def test_mise_lane_comment(self):
        self.assertEqual(targets("mise run lane-comment --issue 1999 --file /tmp/c.md"),
                         [(HRSE, 1999)])

    def test_gh_issue_comment_number_url_and_cwd_default(self):
        self.assertEqual(targets("gh issue comment 42 --body x", cwd_repo=FORGE), [(FORGE, 42)])
        self.assertEqual(targets("gh issue comment 42 -R vitalharmony/hrse --body-file f"),
                         [(HRSE, 42)])
        self.assertEqual(targets("gh issue comment https://github.com/vitalharmony/hrse/issues/7 -b x"),
                         [(HRSE, 7)])

    def test_gh_api_comments_post(self):
        self.assertEqual(targets("gh api repos/vitalharmony/hrse/issues/1830/comments -X POST "
                                 "-f body=@x"), [(HRSE, 1830)])
        self.assertEqual(targets("gh api repos/vitalharmony/hrse/issues/1830/comments "
                                 "-F body=@f.md"), [(HRSE, 1830)])
        self.assertEqual(targets("gh api repos/{owner}/{repo}/issues/5/comments --method POST "
                                 "--input f.json", cwd_repo=FORGE), [(FORGE, 5)])

    def test_gh_api_comment_reads_are_not_posts(self):
        self.assertEqual(targets("gh api repos/vitalharmony/hrse/issues/1830/comments --paginate"), [])
        self.assertEqual(targets("gh api repos/o/r/issues/1/comments -X GET -f per_page=100"), [])

    def test_heredoc_prose_is_not_a_post(self):
        cmd = "cat > /tmp/n.md <<'EOF'\nthen run gh issue comment 12 --body x\nEOF\n"
        self.assertEqual(targets(cmd), [])

    def test_chained_commands(self):
        cmd = "cd /tmp && gh issue comment 3 --body x && l1_post.py --issue 4 --kind ae --sha s --branch b"
        self.assertEqual(targets(cmd, cwd_repo=FORGE), [(FORGE, 3), (HRSE, 4)])


def tool_use(uid, command, model="claude-sonnet-5"):
    return {"type": "assistant", "message": {"role": "assistant", "model": model, "content": [
        {"type": "tool_use", "id": uid, "name": "Bash", "input": {"command": command}}]}}


def tool_result(uid, is_error=False):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": uid, "content": "ok", "is_error": is_error}]}}


def prompt(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


class TurnTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def transcript(self, *entries):
        path = self.root / "t.jsonl"
        path.write_text("".join(json.dumps(e) + "\n" for e in entries))
        return str(path)

    def run_hook(self, path, tiers, env=None):
        calls = []

        def lookup(repo, number):
            calls.append((repo, number))
            value = tiers.get((repo, number))
            if value == "FAIL":
                return model_tier_gate.LOOKUP_FAILED, "HTTP 403"
            return value, None

        with patch.object(model_tier_gate, "resolve_repo", return_value=HRSE):
            out = b.run({"transcript_path": path, "cwd": "/cwd"},
                        env=env if env is not None else {"LANE": "2"},
                        lookup=lookup, fallback_model="claude-sonnet-5")
        return out, calls

    def test_deep_post_from_sonnet_is_reported(self):
        """TC6 offline."""
        path = self.transcript(
            prompt("Plan H1830"),
            tool_use("a", "python3 tools/gh/l2_post.py post --kind plan --issue 1830 "
                          "--repo vitalharmony/hrse --narrative-file n.md"),
            tool_result("a"),
        )
        out, _ = self.run_hook(path, {(HRSE, 1830): "deep"})
        self.assertEqual(
            out["systemMessage"],
            "Posted on vitalharmony/hrse#1830 (Tier deep) from claude-sonnet-5; this needs "
            "redoing on a high-tier model. (harmonic-forge#656)")
        self.assertNotIn("decision", out, "the backstop never blocks")
        self.assertNotIn("LANE_MODEL", out["systemMessage"])

    def test_only_the_current_turn_counts(self):
        path = self.transcript(
            prompt("Plan H1830"),
            tool_use("a", "gh issue comment 1830 --body x"), tool_result("a"),
            prompt("thanks"),
            tool_use("b", "git status"), tool_result("b"),
        )
        out, calls = self.run_hook(path, {(HRSE, 1830): "deep"})
        self.assertIsNone(out)
        self.assertEqual(calls, [])

    def test_failed_post_is_ignored(self):
        path = self.transcript(prompt("x"), tool_use("a", "gh issue comment 1830 --body x"),
                               tool_result("a", is_error=True))
        out, _ = self.run_hook(path, {(HRSE, 1830): "deep"})
        self.assertIsNone(out)

    def test_post_from_opus_is_not_reported_and_not_read(self):
        path = self.transcript(prompt("x"),
                               tool_use("a", "gh issue comment 1830 --body x", model="claude-opus-5"),
                               tool_result("a"))
        out, calls = self.run_hook(path, {(HRSE, 1830): "deep"})
        self.assertIsNone(out)
        self.assertEqual(calls, [])

    def test_non_deep_post_is_silent(self):
        path = self.transcript(prompt("x"), tool_use("a", "gh issue comment 1830 --body x"),
                               tool_result("a"))
        out, _ = self.run_hook(path, {(HRSE, 1830): "standard"})
        self.assertIsNone(out)

    def test_unreadable_tier_is_reported_as_unconfirmed(self):
        path = self.transcript(prompt("x"), tool_use("a", "gh issue comment 1830 --body x"),
                               tool_result("a"))
        out, _ = self.run_hook(path, {(HRSE, 1830): "FAIL"})
        self.assertIn("could not be read (HTTP 403)", out["systemMessage"])

    def test_lane3_is_skipped(self):
        path = self.transcript(prompt("x"), tool_use("a", "gh issue comment 1830 --body x"),
                               tool_result("a"))
        out, calls = self.run_hook(path, {(HRSE, 1830): "deep"}, env={"LANE": "3"})
        self.assertIsNone(out)
        self.assertEqual(calls, [])

    def test_truncated_scan_is_reported(self):
        """Preclose fix 6: a post early in a long turn, then more output than
        the scan bound, used to produce no report at all."""
        filler = [tool_use(f"f{i}", "echo " + "x" * 2000) for i in range(20)]
        path = self.transcript(
            prompt("Plan H1830"),
            tool_use("a", "gh issue comment 1830 --body x"), tool_result("a"),
            *filler,
            tool_use("z", "gh issue comment 1831 --body x"), tool_result("z"),
        )
        with patch.object(b, "_SCAN_MAX_BYTES", 8192), patch.object(b, "_SCAN_CHUNK_BYTES", 4096):
            out, calls = self.run_hook(path, {(HRSE, 1830): "deep", (HRSE, 1831): "deep"})
        self.assertIn("backstop scan truncated; earlier posts in this turn were not checked",
                      out["systemMessage"])
        self.assertIn("vitalharmony/hrse#1831 (Tier deep)", out["systemMessage"])
        self.assertEqual(calls, [(HRSE, 1831)])

    def test_truncated_scan_with_no_posts_found_still_reports(self):
        filler = [tool_use(f"f{i}", "echo " + "x" * 2000) for i in range(20)]
        path = self.transcript(prompt("x"), *filler)
        with patch.object(b, "_SCAN_MAX_BYTES", 8192), patch.object(b, "_SCAN_CHUNK_BYTES", 4096):
            out, _ = self.run_hook(path, {})
        self.assertIn("backstop scan truncated", out["systemMessage"])

    def test_scan_that_reaches_turn_start_is_not_truncated(self):
        filler = [tool_use(f"f{i}", "echo " + "x" * 2000) for i in range(20)]
        path = self.transcript(*filler, prompt("x"), tool_use("a", "git status"))
        with patch.object(b, "_SCAN_MAX_BYTES", 8192), patch.object(b, "_SCAN_CHUNK_BYTES", 4096):
            out, _ = self.run_hook(path, {})
        self.assertIsNone(out)

    def test_whole_short_file_is_not_truncated(self):
        path = self.transcript(tool_use("a", "git status"))
        out, _ = self.run_hook(path, {})
        self.assertIsNone(out)

    def test_mixed_case_repo_flag_is_checked(self):
        path = self.transcript(prompt("x"),
                               tool_use("a", "gh issue comment 1830 -R VitalHarmony/HRSE --body x"),
                               tool_result("a"))
        out, calls = self.run_hook(path, {(HRSE, 1830): "deep"})
        self.assertEqual(calls, [(HRSE, 1830)])
        self.assertIn("Tier deep", out["systemMessage"])

    def test_real_lookup_reads_fresh(self):
        """Preclose fix 7: the backstop reads with ttl=0."""
        path = self.transcript(prompt("x"), tool_use("a", "gh issue comment 1830 --body x"),
                               tool_result("a"))
        seen = {}

        def fake_fetch(repo, issue_number, project_number, **kw):
            seen.update(kw)
            return "deep"

        import tier_model_trigger_check
        with patch.object(model_tier_gate._item_list_cache, "fetch_issue_tier", fake_fetch), \
             patch.object(tier_model_trigger_check, "_boards", return_value={HRSE: "1"}), \
             patch.object(model_tier_gate, "resolve_repo", return_value=HRSE):
            out = b.run({"transcript_path": path, "cwd": "/cwd"}, env={"LANE": "2"},
                        fallback_model="claude-sonnet-5")
        self.assertEqual(seen["ttl"], 0)
        self.assertIn("Tier deep", out["systemMessage"])

    def test_missing_transcript_is_silent(self):
        out, _ = self.run_hook(str(self.root / "missing.jsonl"), {})
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()
