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
        """AC4."""
        prompt = "before\n```\nL2D H1304\n```\nafter"
        result = _run(prompt)
        if result is None:
            return  # no expansion anywhere outside the fence -- also correct
        expanded = result["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("[Lane 2 done", expanded)

    def test_kenekted_prefix_expands_without_owner_slash(self) -> None:
        result = _run("checking K42 status")
        self.assertIsNotNone(result)
        expanded = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("K42 [", expanded)
        self.assertNotIn("K42 [ke'nekted#42]", expanded, "K's repo column has no owner/repo slug form")

    def test_l_prefix_never_treated_as_repo_prefix(self) -> None:
        """The doc reserves `L` and it never appears as a row in the Repo
        prefixes table -- so L2 must not expand as a repo-prefixed issue."""
        result = _run("see L2 for details")
        if result is not None:
            expanded = result["hookSpecificOutput"]["additionalContext"]
            self.assertNotIn("L2 [", expanded)

    def test_plain_prose_with_no_tokens_is_untouched(self) -> None:
        self.assertIsNone(_run("just a normal message with no shorthand at all"))


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

    def test_malformed_doc_fails_open(self) -> None:
        """AC2: an unreadable/malformed doc must not block the prompt."""
        prompt = "L2D H1 whatever"
        with unittest.mock.patch.object(m, "DOC_PATH", Path("/nonexistent/lane-shorthand.md")):
            result = _run(prompt)
        self.assertIsNone(result, "pass-through: no crash, no output, prompt unmodified downstream")


if __name__ == "__main__":
    unittest.main()
