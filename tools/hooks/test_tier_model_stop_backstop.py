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
    def test_l1_post_resolves_the_invoking_checkout_not_hrse(self):
        """harmonic-forge#843 AC1: was `test_l1_post_defaults_to_hrse`."""
        self.assertEqual(targets("python3 scripts/l1_post.py --issue 1830 --kind handoff "
                                 "--sha abc --branch main --file /tmp/h.md", cwd_repo=FORGE),
                         [(FORGE, 1830)])

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
        self.assertEqual(targets(cmd, cwd_repo=FORGE), [(FORGE, 3), (FORGE, 4)])


def tool_use(uid, command, model="claude-sonnet-5"):
    return {"type": "assistant", "message": {"role": "assistant", "model": model, "content": [
        {"type": "tool_use", "id": uid, "name": "Bash", "input": {"command": command}}]}}


def tool_result(uid, is_error=False, content="ok"):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": uid, "content": content, "is_error": is_error}]}}


def receipt(repo, number, comment=1):
    """What `gh issue comment` prints: the created comment's URL (#843 reforge)."""
    return f"https://github.com/{repo}/issues/{number}#issuecomment-{comment}"


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
            tool_result("a", content='{"url": "' + receipt("vitalharmony/hrse", 1830) + '"}'),
        )
        out, _ = self.run_hook(path, {(HRSE, 1830): "deep"})
        self.assertEqual(
            out["systemMessage"],
            "Posted on vitalharmony/hrse#1830 (Tier deep) from claude-sonnet-5; this needs "
            "redoing on a high-tier model. (harmonic-forge#656)")
        self.assertNotIn("decision", out, "the backstop never blocks")
        self.assertNotIn("LANE_MODEL", out["systemMessage"])

    # --- harmonic-forge#843 reforge: receipts, with the NC1 must-be-silent reads ---

    def tool(self, uid, name, inp, model="claude-sonnet-5"):
        return {"type": "assistant", "message": {"role": "assistant", "model": model, "content": [
            {"type": "tool_use", "id": uid, "name": name, "input": inp}]}}

    def test_l1_post_receipt_with_command_substitution_is_flagged(self):
        """The cross-family case that killed the text parser: `$(…)` splits the
        command, but the receipt still names the issue."""
        url = receipt("vitalharmony/hrse", 1908)
        path = self.transcript(prompt("x"),
            tool_use("a", "mise run l1-post --issue 1908 --kind ready-for-l3 "
                          "--sha $(git rev-parse HEAD) --branch b --file f.md"),
            tool_result("a", content=f"[l1-post] $ run\n[l1-post] posted and refetched {url}"))
        out, calls = self.run_hook(path, {(HRSE, 1908): "deep"})
        self.assertEqual(calls, [(HRSE, 1908)])
        self.assertIn("vitalharmony/hrse#1908 (Tier deep)", out["systemMessage"])

    def test_lane_comment_receipt_is_flagged(self):
        url = receipt("vitalharmony/harmonic-forge", 843)
        path = self.transcript(prompt("x"),
            tool_use("a", "mise run lane-comment --repo vitalharmony/harmonic-forge --issue 843 --file f"),
            tool_result("a", content=f"[post-comment] posted and refetched {url}"))
        out, calls = self.run_hook(path, {(FORGE, 843): "deep"})
        self.assertEqual(calls, [(FORGE, 843)])

    def test_a_chained_read_before_a_post_is_not_a_receipt(self):
        """Preclose: `gh api …/comments` JSON (html_url) chained before a post."""
        read_url = receipt("vitalharmony/hrse", 1908)
        post_url = receipt("vitalharmony/hrse", 7)
        path = self.transcript(prompt("x"),
            tool_use("a", "gh api repos/vitalharmony/hrse/issues/1908/comments --jq '.[]' "
                          "&& mise run lane-comment --issue 7 --file f"),
            tool_result("a", content=json.dumps({"html_url": read_url, "body": "x"})
                        + f"\n[post-comment] posted and refetched {post_url}"))
        _out, calls = self.run_hook(path, {(HRSE, 1908): "deep", (HRSE, 7): "fast"})
        self.assertEqual(calls, [(HRSE, 7)])

    def test_implicit_post_gh_api_field_form_is_a_receipt(self):
        url = receipt("vitalharmony/hrse", 1908)
        path = self.transcript(prompt("x"),
            tool_use("a", "gh api repos/vitalharmony/hrse/issues/1908/comments -f body=@n.md --jq .html_url"),
            tool_result("a", content=url))
        _out, calls = self.run_hook(path, {(HRSE, 1908): "deep"})
        self.assertEqual(calls, [(HRSE, 1908)])

    def test_a_bare_gh_post_chained_with_a_read_counts_nothing_from_the_read(self):
        path = self.transcript(prompt("x"),
            tool_use("a", "gh issue comment 7 --body x && gh api repos/vitalharmony/hrse/issues/1908/comments "
                          "--jq '.[].html_url'"),
            tool_result("a", content=receipt("vitalharmony/hrse", 7) + "\n"
                        + receipt("vitalharmony/hrse", 1908)))
        _out, calls = self.run_hook(path, {(HRSE, 1908): "deep", (HRSE, 7): "fast"})
        self.assertNotIn((HRSE, 1908), calls)

    def test_chained_bare_gh_posts_still_count(self):
        """Preclose: 151 real chained posts. Bound to the posting segment's issue."""
        path = self.transcript(prompt("x"),
            tool_use("a", "cd /tmp && gh issue comment 1908 --body-file n.md && echo done"),
            tool_result("a", content=receipt("vitalharmony/hrse", 1908) + "\ndone"))
        _out, calls = self.run_hook(path, {(HRSE, 1908): "deep"})
        self.assertEqual(calls, [(HRSE, 1908)])
        path = self.transcript(prompt("x"),
            tool_use("b", "gh api repos/vitalharmony/hrse/issues/1908/comments -F body=@n.md "
                          "--jq .html_url && rm n.md"),
            tool_result("b", content=receipt("vitalharmony/hrse", 1908)))
        _out, calls = self.run_hook(path, {(HRSE, 1908): "deep"})
        self.assertEqual(calls, [(HRSE, 1908)])

    def test_l2_post_json_anchor_is_l2_post_only_and_url_only(self):
        """Pass-2 #7/#8: a chained read's JSON html_url never counts; l2_post's
        own `url` does; an l2_post JSON line with only html_url does not."""
        read_url, post_url = receipt("vitalharmony/hrse", 1908), receipt("vitalharmony/hrse", 7)
        path = self.transcript(prompt("x"),
            tool_use("a", "gh api repos/vitalharmony/hrse/issues/1908/comments --jq '.[]' "
                          "&& mise run l2-post --kind plan --issue 7 --narrative-file n.md"),
            tool_result("a", content=json.dumps({"html_url": read_url}) + "\n"
                        + json.dumps({"url": post_url, "posted": True})))
        _out, calls = self.run_hook(path, {(HRSE, 1908): "deep", (HRSE, 7): "fast"})
        self.assertEqual(calls, [(HRSE, 7)])
        path = self.transcript(prompt("x"),
            tool_use("b", "python3 tools/gh/l2_post.py post --kind plan --issue 1908"),
            tool_result("b", content=json.dumps({"html_url": read_url})))
        _out, calls = self.run_hook(path, {(HRSE, 1908): "deep"})
        self.assertEqual(calls, [])

    def test_a_url_keyed_json_line_from_a_non_l2_post_command_is_not_a_receipt(self):
        """The l2_post-only gate on its own: a `url` key, not from l2_post."""
        url = receipt("vitalharmony/hrse", 1908)
        path = self.transcript(prompt("x"),
            tool_use("a", "mise run lane-comment --issue 7 --file f && cat result.json"),
            tool_result("a", content=json.dumps({"url": url})))
        _out, calls = self.run_hook(path, {(HRSE, 1908): "deep"})
        self.assertEqual(calls, [])

    def test_a_named_poster_chained_with_a_bare_gh_post_keeps_both(self):
        """Pass-2 #9: no short-circuit; the bare post's URL still counts."""
        path = self.transcript(prompt("x"),
            tool_use("a", "mise run lane-comment --issue 7 --file f && gh issue comment 1908 --body x"),
            tool_result("a", content=f"[post-comment] posted and refetched {receipt('vitalharmony/hrse', 7)}\n"
                        + receipt("vitalharmony/hrse", 1908)))
        _out, calls = self.run_hook(path, {(HRSE, 1908): "deep", (HRSE, 7): "fast"})
        self.assertEqual(sorted(calls), [(HRSE, 7), (HRSE, 1908)])

    def test_receipt_shaped_output_without_a_poster_in_the_command_is_silent(self):
        """Pass-2 #10, NC1 condition 2 on its own."""
        url = receipt("vitalharmony/hrse", 1908)
        path = self.transcript(prompt("x"), tool_use("a", "cat /tmp/session.log"),
                               tool_result("a", content=f"[l1-post] posted and refetched {url}"))
        _out, calls = self.run_hook(path, {(HRSE, 1908): "deep"})
        self.assertEqual(calls, [])

    def test_running_a_posters_tests_mints_no_receipt(self):
        """Pass-2 #3: test output carries fixture receipts."""
        url = receipt("vitalharmony/hrse", 1908)
        path = self.transcript(prompt("x"),
            tool_use("a", "python3 -m unittest tools/gh/test_l1_post.py"),
            tool_result("a", content=f"[l1-post] posted and refetched {url}\nOK"))
        _out, calls = self.run_hook(path, {(HRSE, 1908): "deep"})
        self.assertEqual(calls, [])

    def test_post_flag_before_the_path_is_a_receipt(self):
        """Pass-2 #4."""
        url = receipt("vitalharmony/hrse", 1908)
        for cmd in ("gh api -X POST repos/vitalharmony/hrse/issues/1908/comments -f body=x",
                    "gh api --method POST repos/vitalharmony/hrse/issues/1908/comments -f body=x"):
            with self.subTest(cmd=cmd):
                path = self.transcript(prompt("x"), tool_use("a", cmd), tool_result("a", content=url))
                _out, calls = self.run_hook(path, {(HRSE, 1908): "deep"})
                self.assertEqual(calls, [(HRSE, 1908)])

    def test_list_shaped_tool_result_carries_a_receipt(self):
        url = receipt("vitalharmony/hrse", 1908)
        path = self.transcript(prompt("x"),
            tool_use("a", "mise run lane-comment --issue 1908 --file f"),
            tool_result("a", content=[{"type": "text",
                                       "text": f"[post-comment] posted and refetched {url}"}]))
        _out, calls = self.run_hook(path, {(HRSE, 1908): "deep"})
        self.assertEqual(calls, [(HRSE, 1908)])

    def test_forge_receipt_from_an_hrse_session_is_forge(self):
        url = receipt("vitalharmony/harmonic-forge", 843)
        path = self.transcript(prompt("x"),
            tool_use("a", "python3 tools/gh/post_comment.py --issue 843 --body x"),
            tool_result("a", content=f"[POST-COMMENT] Posted: {url}"))
        _out, calls = self.run_hook(path, {})
        self.assertEqual(calls, [(FORGE, 843)])

    def test_command_text_without_a_receipt_is_silent(self):
        path = self.transcript(prompt("x"),
            tool_use("a", "mise run l2-post --kind plan --issue 1908 --narrative-file n.md"),
            tool_result("a", content="Error: network unreachable"))
        out, calls = self.run_hook(path, {(HRSE, 1908): "deep"})
        self.assertEqual(calls, [])
        self.assertIsNone(out)

    def test_reads_that_quote_a_comment_url_are_never_receipts(self):
        """NC5: the four routine reads, each naming a deep issue's comment URL."""
        url = receipt("vitalharmony/hrse", 1908)
        cases = [
            (self.tool("a", "Read", {"file_path": "/tmp/h.md"}), tool_result("a", content=f"see {url}")),
            (tool_use("a", "python3 ~/harmonic-forge/tools/gh/fetch_lane1_context.py --repo vitalharmony/hrse "
                           "--issue 1908"), tool_result("a", content=f"## Handoff\n{url}")),
            (tool_use("a", "gh api repos/vitalharmony/hrse/issues/1908/comments --jq '.[].html_url'"),
             tool_result("a", content=url)),
            (tool_use("a", "mise run l1-post --issue 7 --kind ae --sha s --branch b --file f"),
             tool_result("a", content=(f"[check-lane3-ready] vitalharmony/hrse#1908: AE ({url}) then sweep\n"
                                       "[l1-post] posted and refetched "
                                       + receipt("vitalharmony/hrse", 7)))),
        ]
        for use, result in cases:
            with self.subTest(tool=use["message"]["content"][0]["input"]):
                path = self.transcript(prompt("x"), use, result)
                _out, calls = self.run_hook(path, {(HRSE, 1908): "deep", (HRSE, 7): "fast"})
                self.assertNotIn((HRSE, 1908), calls)

    def test_only_the_current_turn_counts(self):
        path = self.transcript(
            prompt("Plan H1830"),
            tool_use("a", "gh issue comment 1830 --body x"), tool_result("a", content=receipt("vitalharmony/hrse", 1830)),
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
                               tool_result("a", content=receipt("vitalharmony/hrse", 1830)))
        out, calls = self.run_hook(path, {(HRSE, 1830): "deep"})
        self.assertIsNone(out)
        self.assertEqual(calls, [])

    def test_non_deep_post_is_silent(self):
        path = self.transcript(prompt("x"), tool_use("a", "gh issue comment 1830 --body x"),
                               tool_result("a", content=receipt("vitalharmony/hrse", 1830)))
        out, _ = self.run_hook(path, {(HRSE, 1830): "standard"})
        self.assertIsNone(out)

    def test_unreadable_tier_is_reported_as_unconfirmed(self):
        path = self.transcript(prompt("x"), tool_use("a", "gh issue comment 1830 --body x"),
                               tool_result("a", content=receipt("vitalharmony/hrse", 1830)))
        out, _ = self.run_hook(path, {(HRSE, 1830): "FAIL"})
        self.assertIn("could not be read (HTTP 403)", out["systemMessage"])

    def test_lane3_is_skipped(self):
        path = self.transcript(prompt("x"), tool_use("a", "gh issue comment 1830 --body x"),
                               tool_result("a", content=receipt("vitalharmony/hrse", 1830)))
        out, calls = self.run_hook(path, {(HRSE, 1830): "deep"}, env={"LANE": "3"})
        self.assertIsNone(out)
        self.assertEqual(calls, [])

    def test_truncated_scan_is_reported(self):
        """Preclose fix 6: a post early in a long turn, then more output than
        the scan bound, used to produce no report at all."""
        filler = [tool_use(f"f{i}", "echo " + "x" * 2000) for i in range(20)]
        path = self.transcript(
            prompt("Plan H1830"),
            tool_use("a", "gh issue comment 1830 --body x"), tool_result("a", content=receipt("vitalharmony/hrse", 1830)),
            *filler,
            tool_use("z", "gh issue comment 1831 --body x"), tool_result("z", content=receipt("vitalharmony/hrse", 1831)),
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
                               tool_result("a", content=receipt("VitalHarmony/HRSE", 1830)))
        out, calls = self.run_hook(path, {(HRSE, 1830): "deep"})
        self.assertEqual(calls, [(HRSE, 1830)])
        self.assertIn("Tier deep", out["systemMessage"])

    def test_real_lookup_reads_fresh(self):
        """Preclose fix 7: the backstop reads with ttl=0."""
        path = self.transcript(prompt("x"), tool_use("a", "gh issue comment 1830 --body x"),
                               tool_result("a", content=receipt("vitalharmony/hrse", 1830)))
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
