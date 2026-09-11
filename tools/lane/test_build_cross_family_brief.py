"""harmonic-forge#598 AC4 — the brief handed to a cross-family reviewer is
reusable and self-contained by construction, not by the caller remembering.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent / "build_cross_family_brief.py"
_spec = importlib.util.spec_from_file_location("build_cross_family_brief", _MODULE_PATH)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


class TestRequiredSections(unittest.TestCase):
    def test_every_missing_section_is_named_at_once(self):
        """Not one at a time: a caller who fills in the first and re-runs to
        discover the second has spent two round trips on one mistake."""
        with self.assertRaises(ValueError) as caught:
            m.build([], "", "", [])
        message = str(caught.exception)
        for expected in ("--artifact", "--intent", "--question", "--assumption",
                         "--evidence"):
            self.assertIn(expected, message)

    def test_whitespace_only_is_not_a_filled_section(self):
        with self.assertRaises(ValueError) as caught:
            m.build([("a.md", "x")], "   ", "\n\t ", ["  "], ["p"])
        message = str(caught.exception)
        self.assertIn("--intent", message)
        self.assertIn("--question", message)
        self.assertIn("--assumption", message)

    def test_zero_assumptions_is_refused_because_verify_produces_nothing(self):
        with self.assertRaises(ValueError) as caught:
            m.build([("a.md", "x")], "intent", "question", [], ["p"])
        self.assertIn("--assumption", str(caught.exception))
        self.assertNotIn("--intent", str(caught.exception))


class TestRenderedBrief(unittest.TestCase):
    def _brief(self) -> str:
        return m.build(
            [("tools/x.py", "def f():\n    return 1\n")],
            "make f return the count, not a constant",
            "does f actually read the count from anywhere?",
            ["f reads the count from the manifest"],
            ["tools/x.py"],
        )

    def test_artifact_contents_are_embedded_not_merely_cited(self):
        """The self-containment property AC4 asks for. A brief that names a
        path degrades to 'go find out what I meant' the moment the path
        moves."""
        brief = self._brief()
        self.assertIn("def f():", brief)
        self.assertIn("return 1", brief)

    def test_all_four_sections_are_present_and_labelled(self):
        brief = self._brief()
        self.assertIn("## Design intent", brief)
        self.assertIn("## The question", brief)
        self.assertIn("## Asserted assumptions", brief)
        self.assertIn("## The artifact", brief)

    def test_assumptions_are_numbered_in_the_order_given(self):
        """`verify`'s envelope returns one entry per assumption 'in the same
        order' — the numbering is what makes that mapping checkable."""
        brief = m.build([("a.md", "x")], "i", "q", ["first claim", "second claim"], ["p"])
        self.assertLess(brief.index("1. first claim"), brief.index("2. second claim"))

    def test_the_brief_states_the_reviewer_is_cold(self):
        self.assertIn("cold", self._brief())


class TestCli(unittest.TestCase):
    def test_missing_artifact_file_is_a_usage_error_not_an_empty_brief(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "brief.md"
            code = m.main([
                "--artifact", str(Path(tmp) / "nope.md"),
                "--intent", "i", "--question", "q", "--assumption", "a",
                "--evidence", "p",
                "--out", str(out),
            ])
            self.assertEqual(2, code)
            self.assertFalse(out.exists(), "a refused build must not leave a partial brief")

    def test_a_complete_invocation_writes_the_brief(self):
        with tempfile.TemporaryDirectory() as tmp:
            art = Path(tmp) / "a.md"
            art.write_text("ARTIFACT BODY\n")
            out = Path(tmp) / "brief.md"
            with contextlib.redirect_stdout(io.StringIO()) as captured:
                code = m.main([
                    "--artifact", str(art),
                    "--intent", "i", "--question", "q", "--assumption", "a",
                "--evidence", "p",
                    "--out", str(out),
                ])
            self.assertEqual(0, code)
            self.assertEqual(str(out), captured.getvalue().strip(),
                             "the written path is the CLI's stdout contract")
            self.assertIn("ARTIFACT BODY", out.read_text())



class TestEmptyArtifactBody(unittest.TestCase):
    """harmonic-forge#598 — found live by the cross-family reviewer on this
    mechanism's first real invocation, not by its author. An artifact file
    that exists but holds nothing is the same failure as supplying no
    artifact: the reviewer receives a labelled empty code fence and has to
    grade against nothing."""

    def test_empty_artifact_body_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            m.build([("a.md", "")], "intent", "question", ["claim"], ["p"])
        self.assertIn("a.md", str(caught.exception))
        self.assertIn("empty", str(caught.exception))

    def test_whitespace_only_artifact_body_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            m.build([("a.md", "  \n\t\n")], "intent", "question", ["claim"], ["p"])
        self.assertIn("a.md", str(caught.exception))

    def test_the_offending_artifact_is_named_not_just_counted(self):
        """Two artifacts, one empty: the message must say which."""
        with self.assertRaises(ValueError) as caught:
            m.build([("full.md", "body"), ("hollow.md", "")], "i", "q", ["c"], ["p"])
        message = str(caught.exception)
        self.assertIn("hollow.md", message)
        self.assertNotIn("full.md", message)

    def test_a_nonempty_artifact_still_builds(self):
        self.assertIn("body", m.build([("a.md", "body")], "i", "q", ["c"], ["p"]))

if __name__ == "__main__":
    unittest.main()


class TestEvidencePaths(unittest.TestCase):
    """harmonic-forge#598 preclose finding 1. Embedding the artifact makes the
    brief readable cold; it does not make the assumptions checkable. Without
    an evidence anchor the reviewer has nothing to run, every verdict comes
    back `uncheckable`, and the call exits 0 with `status: ok` having checked
    nothing — a success-shaped envelope carrying no cross-family review."""

    def test_a_brief_with_no_evidence_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            m.build([("a.md", "x")], "i", "q", ["claim"], [])
        self.assertIn("--evidence", str(caught.exception))

    def test_whitespace_only_evidence_is_not_evidence(self):
        with self.assertRaises(ValueError) as caught:
            m.build([("a.md", "x")], "i", "q", ["claim"], ["   "])
        self.assertIn("--evidence", str(caught.exception))

    def test_the_evidence_section_is_rendered_with_its_entries(self):
        brief = m.build([("a.md", "x")], "i", "q", ["claim"],
                        ["tools/hooks/deny.py", "git log -1"])
        self.assertIn("## Where the evidence is", brief)
        self.assertIn("tools/hooks/deny.py", brief)
        self.assertIn("git log -1", brief)

    def test_the_evidence_section_says_a_bare_verdict_is_discarded(self):
        """Mirrors what `emit_envelope` actually does, so the brief does not
        promise the reviewer something the helper will overrule."""
        brief = m.build([("a.md", "x")], "i", "q", ["c"], ["path"])
        self.assertIn("discarded", brief)
