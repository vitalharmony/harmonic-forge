#!/usr/bin/env python3
"""Unit tests for expand_lane_shorthand.py (harmonic-forge#383)."""
import contextlib
import io
import json
import sys
import unittest
import unittest.mock
import tempfile
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import expand_lane_shorthand as m


def _run(prompt: str) -> dict | None:
    """Run the hook end to end, with the authorization store REDIRECTED.

    `main()` creates real BATCH authorizations (harmonic-forge#502), so a test
    feeding it a BATCH prompt writes to the operator's live store unless the
    path is redirected. This is done in the shared helper rather than in the
    one test that noticed, so every existing and future `main()` test is safe
    by construction — `test_batch_gloss_names_what_it_authorizes` passes
    `BATCH H767,F316` and silently extended two long-expired August grants by
    twelve hours before this guard existed.
    """
    import batch_auth as ba

    payload = {"prompt": prompt}
    out = io.StringIO()
    store = Path(tempfile.mkdtemp()) / "batch-authorized.json"
    with unittest.mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
         unittest.mock.patch.object(ba, "STATE_PATH", store), \
         contextlib.redirect_stdout(out):
        m.main()
    text = out.getvalue().strip()
    return json.loads(text) if text else None


class RealDocTests(unittest.TestCase):
    """AC1/AC3/AC4 against the actual rules/lane-shorthand.md, not a fixture --
    proves the parser works against the real doc, not an idealized shape."""

    def test_lane_token_and_repo_token_both_expand(self) -> None:
        result = _run("L2D H1304 please review")
        self.assertIsNotNone(result)
        expanded = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("L2D [Lane 2 done", expanded)
        self.assertIn("H1304 [vitalharmony/hrse#1304]", expanded)

    def test_blocked_template_token_expands_for_every_lane(self) -> None:
        """AC1: L<N>B is a metavariable row in the doc, not a literal
        token -- L1B/L2B/L3B must each expand, not just the literal
        string 'L<N>B' nobody types."""
        for token in ("L1B", "L2B", "L3B"):
            with self.subTest(token=token):
                expanded = m.annotate(f"{token} status", Path(m.DOC_PATH).read_text())
                self.assertIn(f"{token} [Lane N is **blocked** — it could not run]", expanded)

    def test_original_text_preserved_verbatim(self) -> None:
        """AC3: additive annotation only -- the literal prompt text must
        still be findable, unmutated, inside the expanded output."""
        prompt = "L2D H1304 please review this carefully"
        result = _run(prompt)
        expanded = result["hookSpecificOutput"]["additionalContext"]
        stripped = expanded.split("\n", 1)[1]
        for word in prompt.split():
            self.assertIn(word, stripped)

    def test_token_inside_fenced_code_block_is_not_expanded(self) -> None:
        """AC4. Covers both a lane token AND a repo token inside the fence
        -- a fix that only special-cased one match group would pass a
        weaker version of this test (preclose review, test-honesty lens)."""
        prompt = "before\n```\nL2D H1304\n```\nafter"
        result = _run(prompt)
        if result is None:
            return  # no expansion anywhere outside the fence -- also correct
        expanded = result["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("[Lane 2 done", expanded)
        self.assertNotIn("[vitalharmony/hrse#1304]", expanded)

    def test_single_digit_repo_prefix_in_ordinary_prose_is_not_expanded(self) -> None:
        """AC4: the discrimination the docstring names for the repo-prefix
        branch (2+ digits) must actually hold. H1/H2/F5/P0/P1/O2 are all
        real prose collisions found live in preclose review (all 5 lenses)."""
        for prose in ("bump the H1 heading and the H2 spacing",
                      "press F5 to refresh",
                      "this is a P0 bug, escalate to P1",
                      "the O2 sensor reading"):
            with self.subTest(prose=prose):
                self.assertIsNone(_run(prose), f"{prose!r} should not expand any token")

    def test_two_digit_repo_prefix_still_expands(self) -> None:
        """The 2+-digit floor must not over-correct into never matching
        real issue numbers."""
        result = _run("see H26 for context")
        self.assertIsNotNone(result)
        self.assertIn("H26 [vitalharmony/hrse#26]", result["hookSpecificOutput"]["additionalContext"])

    def test_kenekted_prefix_expands_with_full_account_text_preserved(self) -> None:
        """AC1. Asserts on the actual account CONTENT, not just that a
        bracket exists -- the original test only checked shape and let a
        mangled '**`harmonicarchitect' string pass (preclose review,
        test-honesty lens, 3 independent findings)."""
        result = _run("checking K42 status")
        self.assertIsNotNone(result)
        expanded = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("harmonicarchitect", expanded)
        self.assertNotIn("**", expanded)
        self.assertNotIn("`", expanded)
        self.assertNotIn("K42 [ke'nekted#42]", expanded, "K's repo column has no owner/repo slug form")

    def test_leasepal_prefix_does_not_assert_a_nonexistent_repo_falsely(self) -> None:
        """The P row's account column says the repo does not yet exist --
        that caveat must survive into the gloss, not be dropped in favor
        of a bare 'own' (preclose review, correctness + fail-direction
        lenses)."""
        result = _run("track this under P42")
        self.assertIsNotNone(result)
        expanded = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("does not yet exist", expanded)

    def test_eoq_gloss_is_not_truncated_mid_sentence(self) -> None:
        """The doc's Meaning paragraph hard-wraps; a non-DOTALL capture
        truncates mid-sentence (preclose review, 3 of 5 lenses, live-
        reproduced dangling 'It')."""
        result = _run("EOQ merge the doc fix for #334")
        self.assertIsNotNone(result)
        expanded = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("finish everything currently in flight first, then do this.", expanded)
        self.assertNotRegex(expanded, r"\bIt\]")

    def test_now_gloss_expands_only_at_line_start(self) -> None:
        """harmonic-forge#569 preclose review, live-reproduced: `NOW` is an
        ordinary English word, unlike `EOQ`/`BATCH`, so an unanchored
        `\\bNOW\\b` match injected the interrupt gloss into prose that
        merely discussed or quoted the token -- a docstring reference and a
        blockquoted sentence both got rewritten as a live interrupt
        instruction. Anchoring the directive match to the start of the
        line (its own documented grammar: `NOW` + trailing instruction)
        fixes both without touching the genuine leading-`NOW` case."""
        quoting = (
            "Preclose review. The issue says: requires an explicit `NOW` "
            "token or Esc.\n> I very rarely want to interrupt. NOW is the "
            "marker.\n"
        )
        result = _run(quoting)
        if result is not None:
            expanded = result["hookSpecificOutput"]["additionalContext"]
            self.assertNotIn("Abandon or suspend", expanded)
        else:
            self.assertIsNone(result)

        result = _run("NOW stop and look at this")
        self.assertIsNotNone(result)
        expanded = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Abandon or suspend current work", expanded)

    def test_batch_gloss_names_what_it_authorizes(self) -> None:
        result = _run("BATCH H767,F316")
        self.assertIsNotNone(result)
        expanded = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("issues", expanded)
        self.assertIn("gh pr merge", expanded)

    def test_l_prefix_never_treated_as_repo_prefix(self) -> None:
        """Directly asserts on the parsed prefix set (not on output that,
        when correct, simply doesn't exist) -- the doc reserves `L` and it
        never appears as a row in the Repo prefixes table. The original
        version of this test executed zero assertions when correct
        (preclose review, test-honesty lens)."""
        doc_text = Path(m.DOC_PATH).read_text()
        self.assertNotIn("L", m.parse_repo_prefixes(doc_text))

    def test_plain_prose_with_no_tokens_is_untouched(self) -> None:
        self.assertIsNone(_run("just a normal message with no shorthand at all"))

    def test_malformed_row_with_empty_account_cell_does_not_crash_other_tokens(self) -> None:
        """A doc row shaped like the P row but with a genuinely empty
        Account cell must degrade gracefully for its own token and must
        NOT poison expansion of unrelated, well-formed tokens in the same
        prompt (preclose review, correctness lens, live-reproduced
        IndexError swallowed by the outer fail-open, blanking the whole
        prompt)."""
        doc_text = Path(m.DOC_PATH).read_text()
        broken_doc = doc_text.replace(
            "| `P` | LeasePAL | own account — **projected, repo does not yet exist** |",
            "| `P` | LeasePAL |  |",
        )
        expanded = m.annotate("L2D H26 and P99", broken_doc)
        self.assertIn("L2D [Lane 2 done", expanded)
        self.assertIn("H26 [vitalharmony/hrse#26]", expanded)
        self.assertIn("P99 [LeasePAL issue #99 (account: unknown account)]", expanded)


class ParserFixtureTests(unittest.TestCase):
    """AC2: a row added to the doc expands with no hook change -- proven
    against a synthetic doc fixture the parser has never seen."""

    FIXTURE = """# Lane shorthand

## Lane status tokens

| Token | Meaning | Direction |
|---|---|---|
| `L9Z` | a brand new made-up token | lane -> operator |

## Repo prefixes

| Prefix | Repo | Account |
|---|---|---|
| `Q` | `vitalharmony/quux` | vitalharmony |
"""

    def test_new_row_expands_with_no_code_change(self) -> None:
        expanded = m.annotate("L9Z Q99 test", self.FIXTURE)
        self.assertIn("L9Z [a brand new made-up token]", expanded)
        self.assertIn("Q99 [vitalharmony/quux#99]", expanded)

    def test_malformed_doc_fails_open_on_missing_file(self) -> None:
        """AC2: an unreadable doc must not block the prompt."""
        prompt = "L2D H26 whatever"
        with unittest.mock.patch.object(m, "DOC_PATH", Path("/nonexistent/lane-shorthand.md")):
            result = _run(prompt)
        self.assertIsNone(result, "pass-through: no crash, no output, prompt unmodified downstream")

    def test_malformed_doc_fails_open_on_missing_headings(self) -> None:
        """AC2 names 'malformed', not just 'unreadable' -- a doc that
        exists but has no recognizable table structure at all must also
        fail open, via a genuinely different code path (build_annotator
        returning None) than the missing-file case above (preclose
        review, test-honesty lens: the two cases were previously
        indistinguishable because only the missing-file path was tested)."""
        garbage_doc = "# Not a real lane-shorthand doc\n\njust some prose with no tables.\n"
        self.assertEqual(m.annotate("L2D H26 whatever", garbage_doc), "L2D H26 whatever")

    def test_main_actually_blocks_nothing_with_the_real_doc_restored(self) -> None:
        """Pairs with the fail-open tests above: proves the SAME prompt
        does expand once the doc is healthy again, so 'no output' in the
        fail-open tests is demonstrated to mean 'skipped', not 'hook is
        permanently broken' (preclose review, test-honesty lens)."""
        result = _run("L2D H26 whatever")
        self.assertIsNotNone(result)


def _fake_gh_result(returncode=0, stdout="", stderr=""):
    result = unittest.mock.Mock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


class LiveIssueReReadTests(unittest.TestCase):
    """harmonic-forge#397: a real repo-prefixed issue reference must
    trigger a live (uncached) `gh issue view` fetch, injected alongside
    the existing inline-gloss expansion."""

    def _issue_json(self, **overrides):
        data = {
            "title": "Some issue title",
            "state": "OPEN",
            "updatedAt": "2026-08-27T00:00:00Z",
            "body": "The full current body.",
            "comments": [
                {"author": {"login": "marcmangus"}, "createdAt": "2026-08-27T01:00:00Z", "body": "A comment."},
            ],
        }
        data.update(overrides)
        return json.dumps(data)

    def test_real_repo_ref_triggers_live_fetch_with_full_body_and_comments(self):
        with unittest.mock.patch.object(
            m.subprocess, "run", return_value=_fake_gh_result(stdout=self._issue_json())
        ) as run:
            result = _run("Implement H1304")
        self.assertIsNotNone(result)
        expanded = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("The full current body.", expanded)
        self.assertIn("A comment.", expanded)
        self.assertIn("marcmangus", expanded)
        run.assert_called_once()
        args = run.call_args[0][0]
        self.assertEqual(args[:3], ["gh", "issue", "view"])
        self.assertIn("1304", args)
        self.assertIn("vitalharmony/hrse", args)

    def test_fires_on_continuation_shaped_trigger_not_just_fresh_implement(self):
        """AC3: 'continue'/'unblocked'-shaped prompts must fetch live too --
        the match is on the token, not the surrounding verb."""
        for prompt in ("continue H1304", "H1304 unblocked", "unblocked, H1304"):
            with self.subTest(prompt=prompt):
                with unittest.mock.patch.object(
                    m.subprocess, "run", return_value=_fake_gh_result(stdout=self._issue_json())
                ) as run:
                    result = _run(prompt)
                self.assertIsNotNone(result)
                run.assert_called_once()

    def test_no_live_fetch_when_no_repo_ref_present(self):
        with unittest.mock.patch.object(m.subprocess, "run") as run:
            result = _run("L2D status update, no issue mentioned")
        self.assertIsNotNone(result)
        run.assert_not_called()

    def test_account_only_prefix_is_not_live_fetched(self):
        """K/P have no owner/repo shorthand -- nothing to `gh issue view`."""
        with unittest.mock.patch.object(m.subprocess, "run") as run:
            result = _run("checking K42 status")
        self.assertIsNotNone(result)  # inline gloss still fires
        run.assert_not_called()

    def test_fetch_failure_fails_open_with_explicit_marker_not_a_crash(self):
        with unittest.mock.patch.object(
            m.subprocess, "run", return_value=_fake_gh_result(returncode=1, stderr="not found")
        ):
            result = _run("Implement H1304")
        self.assertIsNotNone(result)
        expanded = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("live fetch failed", expanded)

    def test_timeout_fails_open_with_explicit_marker(self):
        with unittest.mock.patch.object(
            m.subprocess, "run", side_effect=m.subprocess.TimeoutExpired(cmd="gh", timeout=8)
        ):
            result = _run("Implement H1304")
        self.assertIsNotNone(result)
        self.assertIn("live fetch failed", result["hookSpecificOutput"]["additionalContext"])

    def test_malformed_json_fails_open_with_explicit_marker(self):
        with unittest.mock.patch.object(
            m.subprocess, "run", return_value=_fake_gh_result(stdout="not json")
        ):
            result = _run("Implement H1304")
        self.assertIsNotNone(result)
        self.assertIn("live fetch failed", result["hookSpecificOutput"]["additionalContext"])

    def test_two_distinct_issue_refs_each_fetched_once(self):
        with unittest.mock.patch.object(
            m.subprocess, "run", return_value=_fake_gh_result(stdout=self._issue_json())
        ) as run:
            result = _run("H1304 and F383 both relevant")
        self.assertIsNotNone(result)
        self.assertEqual(run.call_count, 2)

    def test_repeated_ref_in_same_prompt_fetched_only_once(self):
        with unittest.mock.patch.object(
            m.subprocess, "run", return_value=_fake_gh_result(stdout=self._issue_json())
        ) as run:
            result = _run("H1304, see also H1304 again")
        self.assertIsNotNone(result)
        run.assert_called_once()

    def test_not_cached_across_separate_invocations(self):
        """AC2 'not cached' -- a second, separate hook invocation for the
        same issue must fetch live again, not reuse a prior result."""
        with unittest.mock.patch.object(
            m.subprocess, "run", return_value=_fake_gh_result(stdout=self._issue_json())
        ) as run:
            _run("Implement H1304")
            _run("Implement H1304")
        self.assertEqual(run.call_count, 2)


class LiveIssueContextSizeCaps(unittest.TestCase):
    """harmonic-forge#397/#399 preclose-inspection finding, live-reproduced
    at ~93k characters for one real issue: an unbounded fetch, re-injected
    in full on every continuation trigger, is unbounded context growth."""

    def test_long_body_is_truncated_with_explicit_marker(self):
        long_body = "x" * (m._BODY_CHAR_CAP + 500)
        with unittest.mock.patch.object(
            m.subprocess, "run",
            return_value=_fake_gh_result(stdout=json.dumps({
                "title": "t", "state": "OPEN", "updatedAt": "2026-01-01T00:00:00Z",
                "body": long_body, "comments": [],
            })),
        ):
            block = m.fetch_issue_context("vitalharmony/hrse", "1")
        self.assertLess(len(block), len(long_body))
        self.assertIn("truncated", block)

    def test_short_body_is_not_truncated(self):
        with unittest.mock.patch.object(
            m.subprocess, "run",
            return_value=_fake_gh_result(stdout=json.dumps({
                "title": "t", "state": "OPEN", "updatedAt": "2026-01-01T00:00:00Z",
                "body": "short", "comments": [],
            })),
        ):
            block = m.fetch_issue_context("vitalharmony/hrse", "1")
        self.assertIn("short", block)
        self.assertNotIn("truncated", block)

    def test_many_comments_shows_only_most_recent_with_omission_count(self):
        comments = [
            {"author": {"login": "u"}, "createdAt": f"2026-01-{i:02d}T00:00:00Z", "body": f"comment {i}"}
            for i in range(1, 21)
        ]
        with unittest.mock.patch.object(
            m.subprocess, "run",
            return_value=_fake_gh_result(stdout=json.dumps({
                "title": "t", "state": "OPEN", "updatedAt": "2026-01-01T00:00:00Z",
                "body": "b", "comments": comments,
            })),
        ):
            block = m.fetch_issue_context("vitalharmony/hrse", "1")
        # Most recent comments (highest-numbered) must be present...
        self.assertIn("comment 20", block)
        self.assertIn("comment 13", block)  # last 8 of 20 = comments 13-20
        # ...oldest ones must be dropped, not the newest.
        self.assertNotIn("comment 1\n", block)
        self.assertNotIn("comment 12", block)
        self.assertIn("12 earlier omitted", block)

    def test_few_comments_shows_all_with_no_omission_note(self):
        comments = [
            {"author": {"login": "u"}, "createdAt": "2026-01-01T00:00:00Z", "body": "only one"},
        ]
        with unittest.mock.patch.object(
            m.subprocess, "run",
            return_value=_fake_gh_result(stdout=json.dumps({
                "title": "t", "state": "OPEN", "updatedAt": "2026-01-01T00:00:00Z",
                "body": "b", "comments": comments,
            })),
        ):
            block = m.fetch_issue_context("vitalharmony/hrse", "1")
        self.assertIn("only one", block)
        self.assertNotIn("omitted", block)

    def test_long_individual_comment_is_truncated(self):
        long_comment = "y" * (m._COMMENT_CHAR_CAP + 500)
        with unittest.mock.patch.object(
            m.subprocess, "run",
            return_value=_fake_gh_result(stdout=json.dumps({
                "title": "t", "state": "OPEN", "updatedAt": "2026-01-01T00:00:00Z",
                "body": "b",
                "comments": [{"author": {"login": "u"}, "createdAt": "2026-01-01T00:00:00Z", "body": long_comment}],
            })),
        ):
            block = m.fetch_issue_context("vitalharmony/hrse", "1")
        self.assertLess(len(block), len(long_comment))
        self.assertIn("truncated", block)

    def test_end_to_end_injected_block_stays_well_under_the_measured_regression_size(self):
        """The concrete regression this exists to prevent: ~93k chars for
        one real issue. Worst case (max body + max comments, all at cap)
        must stay a small fraction of that."""
        comments = [
            {"author": {"login": "u"}, "createdAt": "2026-01-01T00:00:00Z", "body": "z" * m._COMMENT_CHAR_CAP}
            for _ in range(30)
        ]
        with unittest.mock.patch.object(
            m.subprocess, "run",
            return_value=_fake_gh_result(stdout=json.dumps({
                "title": "t", "state": "OPEN", "updatedAt": "2026-01-01T00:00:00Z",
                "body": "x" * m._BODY_CHAR_CAP, "comments": comments,
            })),
        ):
            block = m.fetch_issue_context("vitalharmony/hrse", "1")
        self.assertLess(len(block), 20000)


class LiveIssueAggregateFetchCap(unittest.TestCase):
    """harmonic-forge#397/#399 preclose-inspection finding, round 2, live-
    reproduced at 243k characters for 25 refs in one prompt: the per-issue
    cap alone doesn't bound a multi-issue prompt. `_MAX_LIVE_FETCHES`
    bounds both aggregate size and worst-case wall time."""

    def _issue_json(self):
        return json.dumps({
            "title": "t", "state": "OPEN", "updatedAt": "2026-01-01T00:00:00Z",
            "body": "b", "comments": [],
        })

    def test_refs_beyond_the_cap_are_not_fetched(self):
        prompt = " ".join(f"H130{i}" for i in range(9))  # 9 distinct real-shaped refs
        with unittest.mock.patch.object(
            m.subprocess, "run", return_value=_fake_gh_result(stdout=self._issue_json())
        ) as run:
            result = _run(prompt)
        self.assertIsNotNone(result)
        self.assertEqual(run.call_count, m._MAX_LIVE_FETCHES)

    def test_skipped_refs_get_an_explicit_not_fetched_marker(self):
        prompt = " ".join(f"H130{i}" for i in range(9))
        with unittest.mock.patch.object(
            m.subprocess, "run", return_value=_fake_gh_result(stdout=self._issue_json())
        ):
            result = _run(prompt)
        expanded = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("not fetched", expanded)
        self.assertIn(f"more than {m._MAX_LIVE_FETCHES}", expanded)

    def test_refs_at_or_under_the_cap_are_all_fetched(self):
        prompt = " ".join(f"H130{i}" for i in range(m._MAX_LIVE_FETCHES))
        with unittest.mock.patch.object(
            m.subprocess, "run", return_value=_fake_gh_result(stdout=self._issue_json())
        ) as run:
            result = _run(prompt)
        self.assertIsNotNone(result)
        self.assertEqual(run.call_count, m._MAX_LIVE_FETCHES)
        expanded = result["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("not fetched", expanded)

    def test_aggregate_size_stays_bounded_regardless_of_ref_count_named(self):
        """The concrete regression: 25 refs measured at 243k chars live.
        Worst case here (cap-many refs, each at its own per-issue cap)
        must stay a small, predictable multiple of one issue's cap."""
        prompt = " ".join(f"H13{i:02d}" for i in range(25))
        big_issue = json.dumps({
            "title": "t", "state": "OPEN", "updatedAt": "2026-01-01T00:00:00Z",
            "body": "x" * m._BODY_CHAR_CAP,
            "comments": [
                {"author": {"login": "u"}, "createdAt": "2026-01-01T00:00:00Z", "body": "z" * m._COMMENT_CHAR_CAP}
                for _ in range(30)
            ],
        })
        with unittest.mock.patch.object(
            m.subprocess, "run", return_value=_fake_gh_result(stdout=big_issue)
        ):
            result = _run(prompt)
        expanded = result["hookSpecificOutput"]["additionalContext"]
        self.assertLess(len(expanded), 80000)


if __name__ == "__main__":
    unittest.main()


class BatchAuthorizationTests(unittest.TestCase):
    """AC1 — typing BATCH must create the authorization with no session action.

    Before this, `authorize()`'s only caller was its own CLI, so the actor
    that had to notice the keyword was the assistant session, from memory,
    every time. It did not: the state file's newest entry was eleven days
    stale while an unattended batch stalled for hours.
    """

    def test_the_operators_actual_message_yields_all_five_keys(self):
        """The message that filed harmonic-forge#502, verbatim. The first cut
        required keys ADJACENT to the token and returned [] for this —
        five keys, four intervening words, zero authorized."""
        prompt = ("BATCH these tooling issues F495, F497, F498, F500, H1631 - "
                  "implement, merge and close and don't stop or wait for HITL "
                  "as the batch definition allows")
        self.assertEqual(m.batch_keys(prompt),
                         ["F495", "F497", "F498", "F500", "H1631"])

    def test_the_terse_form_still_works(self):
        self.assertEqual(m.batch_keys("BATCH F495, F497 H1631"),
                         ["F495", "F497", "H1631"])

    def test_lowercase_batch_in_prose_authorizes_nothing(self):
        for prompt in ("the word batch in prose about F495",
                       "we should batch F495 and F497 someday"):
            with self.subTest(prompt=prompt):
                self.assertEqual(m.batch_keys(prompt), [])

    def test_the_token_alone_authorizes_nothing(self):
        self.assertEqual(m.batch_keys("BATCH"), [])
        self.assertEqual(m.batch_keys("BATCH is broken"), [])

    def test_keys_are_bounded_to_the_batch_line(self):
        """A later paragraph mentioning an unrelated issue is not swept in."""
        self.assertEqual(m.batch_keys("BATCH F495\n\nSeparately, H999 is open."),
                         ["F495"])

    def test_keys_are_deduplicated_in_order(self):
        self.assertEqual(m.batch_keys("BATCH F497 F495 F497"), ["F497", "F495"])

    def test_a_lane_trigger_is_not_a_batch(self):
        for prompt in ("L2B F496", "L3S H745", "Close F495"):
            with self.subTest(prompt=prompt):
                self.assertEqual(m.batch_keys(prompt), [])

    def test_authorize_batch_writes_two_merge_targets_and_no_close(self):
        """A cross-repo issue needs one merge per repo; harmonic-forge#497
        needed two and the single granted slot made the second prompt.
        harmonic-forge#612: no close target at all any more -- closing
        happens via a live-gated `Closes #N`, never a direct BATCH grant."""
        import json as _json
        import batch_auth as ba

        tmp = Path(tempfile.mkdtemp()) / "state.json"
        with mock.patch.object(ba, "STATE_PATH", tmp):
            receipt = m.authorize_batch("BATCH F1, F2", state_path=tmp)
        self.assertIn("F1, F2", receipt)
        state = _json.loads(tmp.read_text())
        for key in ("F1", "F2"):
            actions = [t["action"] for t in state[key]["targets"]]
            self.assertEqual(actions.count("gh pr merge"), 2, key)
            self.assertEqual(actions.count("gh issue close"), 0, key)

    def test_a_non_batch_prompt_writes_nothing_and_returns_empty(self):
        import batch_auth as ba

        tmp = Path(tempfile.mkdtemp()) / "state.json"
        with mock.patch.object(ba, "STATE_PATH", tmp):
            self.assertEqual(
                m.authorize_batch("just a normal message", state_path=tmp), "")
        self.assertFalse(tmp.exists())

    def test_a_failure_is_reported_never_raised(self):
        """This runs on EVERY prompt. A failure here must never cost the
        operator their message."""
        import batch_auth as ba

        tmp = Path(tempfile.mkdtemp()) / "state.json"
        with mock.patch.object(ba, "top_up", side_effect=RuntimeError("boom")):
            receipt = m.authorize_batch("BATCH F1", state_path=tmp)
        self.assertIn("FAILED", receipt)
        self.assertIn("boom", receipt)


class BatchWiringTests(unittest.TestCase):
    """AC1/AC5 asserted, not merely claimed.

    Deleting the `authorize_batch(prompt)` call from `main()` used to leave
    all 47 tests green — the change could ship as a complete no-op.
    """

    def _main(self, prompt: str, state_path: Path) -> tuple[str, bool]:
        import batch_auth as ba

        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps({"prompt": prompt}))), \
                mock.patch.object(sys, "stdout", out), \
                mock.patch.object(ba, "STATE_PATH", state_path):
            m.main()
        return out.getvalue(), state_path.exists()

    def test_main_creates_the_authorization_and_says_so(self) -> None:
        tmp = Path(tempfile.mkdtemp()) / "state.json"
        output, written = self._main("BATCH F495, F497", tmp)
        self.assertTrue(written, "main() did not write the state file")
        self.assertIn("BATCH", output)
        state = json.loads(tmp.read_text())
        self.assertEqual(sorted(state), ["F495", "F497"])

    def test_the_authorization_is_on_disk_before_main_returns(self) -> None:
        """AC5 — the separate-tool-call rule holds structurally because the
        write happens on UserPromptSubmit, before the turn's first tool call.
        Asserted here rather than argued in a comment."""
        tmp = Path(tempfile.mkdtemp()) / "state.json"
        self._main("BATCH F495", tmp)
        targets = json.loads(tmp.read_text())["F495"]["targets"]
        self.assertEqual(sum(1 for t in targets if t["action"] == "gh pr merge"), 2)

    def test_a_doc_read_failure_does_not_skip_authorization(self) -> None:
        """It used to sit after a try/except whose `return` fires when
        `lane-shorthand.md` is momentarily absent — silently skipping the
        authorization, which is the original defect exactly."""
        import batch_auth as ba

        tmp = Path(tempfile.mkdtemp()) / "state.json"
        out = io.StringIO()
        with mock.patch.object(sys, "stdin",
                               io.StringIO(json.dumps({"prompt": "BATCH F495"}))), \
                mock.patch.object(sys, "stdout", out), \
                mock.patch.object(ba, "STATE_PATH", tmp), \
                mock.patch.object(m, "DOC_PATH", Path("/nonexistent/doc.md")):
            m.main()
        self.assertTrue(tmp.exists(), "a missing doc silently skipped authorization")
        self.assertIn("BATCH", out.getvalue())

    def test_a_re_mention_extends_rather_than_resetting_consumption(self) -> None:
        """A mid-batch "keep going on the BATCH F495 work" is encouragement,
        not a new grant. Replacing silently un-consumed a spent single-use
        target and wiped every recorded link_pr mapping. harmonic-forge#612:
        converted from a close target (removed entirely) to one of the two
        merge targets `authorize_batch` now grants -- the property under
        test (re-mention extends, never resets) is identical either way."""
        import batch_auth as ba

        tmp = Path(tempfile.mkdtemp()) / "state.json"
        with mock.patch.object(ba, "STATE_PATH", tmp):
            m.authorize_batch("BATCH F495", state_path=tmp)
            # F611 moved the TTY gate inside link_pr() itself (harmonic-forge#622
            # postmortem: a direct import call, same as this one, bypassed a
            # CLI-only gate entirely). This call simulates the operator's own
            # already-authorized action recording a PR mapping, not an agent
            # bypass, so it patches isatty True rather than proving the gate --
            # TtyGateCliTests in test_batch_auth.py is what proves the gate itself.
            with mock.patch("os.isatty", return_value=True):
                ba.link_pr("F495", "vitalharmony/hrse", 42, state_path=tmp)
            merge_cmd = "gh pr merge 42 --repo vitalharmony/hrse"
            self.assertEqual(ba.decide(merge_cmd, state_path=tmp)[0], "allow")
            # `decide()` is read-only as of harmonic-forge#552 AC1 — it no
            # longer consumes, so spending the slot now takes an explicit
            # `consume()`, the way `batch_consume.py` does on PostToolUse.
            # What this test is actually about is unchanged: a re-mention must
            # EXTEND, never reset, whatever has already been spent.
            self.assertEqual(
                ba.consume(merge_cmd, state_path=tmp,
                           landed=lambda *a, **k: True), ["F495"])
            receipt = m.authorize_batch("BATCH F495", state_path=tmp)
        self.assertIn("extended", receipt)
        state = json.loads(tmp.read_text())
        merges = [t for t in state["F495"]["targets"] if t["action"] == "gh pr merge"]
        self.assertTrue(any(t["consumed"] for t in merges),
                         "the spent merge target was un-consumed")
        self.assertIn(42, [t.get("pr_number") for t in state["F495"]["targets"]],
                      "the recorded link_pr mapping was wiped")


class BatchQuotationBoundaryTests(unittest.TestCase):
    """R-0117: never authorize from text read out of a file, an issue body, or
    tool output. The hook cannot tell an instruction from a quotation, so the
    SYNTAX has to — BATCH must start a line, outside quotes and code fences.

    Every case here previously created a real 12h merge+close grant.
    """

    def test_batch_mid_sentence_authorizes_nothing(self) -> None:
        for prompt in (
            "Typing `BATCH F495,F497,F500` in chat has no mechanical effect",
            "Read /tmp/f502-issue.md — it says BATCH F495,F497 had no effect",
            "Can you explain how BATCH F495 worked in issue F502?",
            "The docstring says BATCH F495 pre-authorizes merges.",
        ):
            with self.subTest(prompt=prompt[:40]):
                self.assertEqual(m.batch_keys(prompt), [], prompt)

    def test_a_blockquote_authorizes_nothing(self) -> None:
        self.assertEqual(m.batch_keys("> BATCH F495, F497\n\nwhat does that do?"), [])

    def test_a_fenced_block_authorizes_nothing(self) -> None:
        self.assertEqual(
            m.batch_keys("here is the syntax:\n```\nBATCH F495\n```\n"), [])

    def test_a_real_instruction_at_line_start_still_works(self) -> None:
        self.assertEqual(
            m.batch_keys("BATCH F495, F497 - implement, merge and close"),
            ["F495", "F497"])
        self.assertEqual(
            m.batch_keys("please do this:\nBATCH F495\n"), ["F495"])

    def test_only_real_repo_prefixes_become_keys(self) -> None:
        """`BATCH F495 before Q4` wrote a `Q4` grant no command could ever
        consume, which then sat pending for the full TTL."""
        self.assertEqual(
            m.batch_keys("BATCH F495, F497 before Q4 and don't stop"),
            ["F495", "F497"])

    def test_lowercase_keys_are_accepted_and_normalized(self) -> None:
        """`BATCH f495` authorized NOTHING and reported nothing — the exact
        silent no-op this issue exists to remove. `authorize()` upper-cases
        keys anyway; the restriction lived only in this regex."""
        self.assertEqual(m.batch_keys("BATCH f495, F497"), ["F495", "F497"])


class ProductionStateIsolationTests(unittest.TestCase):
    """The test suite must never touch the operator's live authorization store.

    It did: `main()` now creates real grants, and a pre-existing test feeding
    it `BATCH H767,F316` extended two long-expired August authorizations by
    twelve hours on the operator's machine. Caught by diffing the real file
    across a suite run, which is what this test automates.
    """

    def test_running_the_hook_never_writes_the_default_store(self) -> None:
        import batch_auth as ba

        real = ba.STATE_PATH
        before = real.read_bytes() if real.exists() else None
        _run("BATCH H767,F316")
        after = real.read_bytes() if real.exists() else None
        self.assertEqual(before, after,
                         f"the suite wrote to the live store at {real}")

    def test_the_helper_redirects_rather_than_relying_on_each_test(self) -> None:
        """Fixing only the test that noticed would leave every future
        `main()` test to remember this on its own."""
        import inspect

        source = inspect.getsource(_run)
        self.assertIn("STATE_PATH", source)
        self.assertIn("mkdtemp", source)
