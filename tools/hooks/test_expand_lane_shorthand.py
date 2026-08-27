#!/usr/bin/env python3
"""Unit tests for expand_lane_shorthand.py (harmonic-forge#383)."""
import contextlib
import io
import json
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import expand_lane_shorthand as m


def _run(prompt: str) -> dict | None:
    payload = {"prompt": prompt}
    out = io.StringIO()
    with unittest.mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
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


if __name__ == "__main__":
    unittest.main()
