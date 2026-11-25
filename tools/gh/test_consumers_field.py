"""The `Consumers and Equivalents` form check (harmonic-forge#1502)."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _consumers_field as C  # noqa: E402

REAL_ID = "harmonic-forge.gh-argv-parser"


class FormTests(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.manifest = self.tmp / "projects.toml"
        self.manifest.write_text('[[project]]\nname = "hrse"\npath = "/nonexistent/hrse"\n')

    def check(self, section: str, **kw) -> str | None:
        kw.setdefault("manifest", self.manifest)
        return C.consumers_refusal(section, **kw)

    # AC1
    def test_a_resolving_id_passes(self) -> None:
        self.assertIsNone(self.check(f"Contract: {REAL_ID}"))

    # AC2
    def test_no_entry_with_a_search_passes(self) -> None:
        self.assertIsNone(self.check("No index entry applies.\nSearch: `git grep -n x`"))

    # AC3 / TC2
    def test_free_prose_refuses_naming_both_forms(self) -> None:
        msg = self.check("none, per the search recorded in the review")
        self.assertIn("Contract: <id>", msg)
        self.assertIn("No index entry applies", msg)

    # AC4 / TC1
    def test_an_unresolved_id_refuses_naming_it(self) -> None:
        msg = self.check("Contract: harmonic-forge.no-such-contract")
        self.assertIn("harmonic-forge.no-such-contract", msg)
        self.assertIn("not in the contract index", msg)

    def test_one_bad_id_among_good_refuses(self) -> None:
        msg = self.check(f"Contract: {REAL_ID}\nContract: harmonic-forge.missing")
        self.assertIn("harmonic-forge.missing", msg)
        self.assertNotIn(REAL_ID, msg)

    # AC5
    def test_a_known_project_without_a_checkout_is_unmeasured_and_passes(self) -> None:
        # checkout injected: the real HRSE2 checkout must not decide this case.
        self.assertIsNone(self.check("Contract: hrse.anything", checkout=lambda _project: None))

    def test_a_backtick_only_search_refuses(self) -> None:
        self.assertIn("Search:", self.check("No index entry applies.\nSearch: ``"))

    def test_a_present_checkout_without_an_index_refuses(self) -> None:
        other = self.tmp / "hrse"
        other.mkdir()
        msg = self.check("Contract: hrse.anything", checkout=lambda _project: other)
        self.assertIn("no readable contract index", msg)

    def test_a_present_checkout_with_a_malformed_index_refuses(self) -> None:
        """Post-verdict finding 3: a malformed foreign index cannot vouch for
        an id, so a present checkout carrying one refuses like a missing one."""
        other = self.tmp / "hrse-bad"
        other.mkdir()
        (other / "contracts.toml").write_text("this is [not toml\n", encoding="utf-8")
        msg = self.check("Contract: hrse.anything", checkout=lambda _project: other)
        self.assertIn("no readable contract index", msg)
        self.assertIn("hrse.anything", msg)

    def test_a_present_checkout_with_a_valid_index_resolves_and_passes(self) -> None:
        """The refusal above is for an unreadable index only: an id the
        present checkout's valid index carries passes."""
        other = self.tmp / "hrse-good"
        other.mkdir()
        (other / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        (other / "contracts.toml").write_text(
            '[[contract]]\nid = "hrse.date-contract"\nscope = "s"\nowner = "a.py: f"\nmodules = ["a.py"]\n'
            'authority = "a.py"\nexample_caller = "a.py:1"\nguard = ["a.py"]\nexceptions = "none"\n'
            'scan_roots = ["."]\nblind_spots = "none"\nstatus = "canonical"\n', encoding="utf-8")
        self.assertIsNone(self.check("Contract: hrse.date-contract", checkout=lambda _project: other))

    def test_backticks_inside_an_id_are_not_stripped(self) -> None:
        self.assertIn("not a contract id", self.check("Contract: harmonic-forge.gh`-argv-parser"))

    def test_crlf_lines_are_read(self) -> None:
        self.assertIsNone(self.check("No index entry applies.\r\nSearch: `rg x`\r\n"))

    def test_backticked_labels_are_read(self) -> None:
        self.assertIsNone(self.check(f"`Contract: {REAL_ID}`"))
        self.assertIsNone(self.check("`No index entry applies`\n`Search: git grep x`"))

    def test_bold_labels_are_read(self) -> None:
        self.assertIsNone(self.check(f"**Contract:** {REAL_ID}"))
        self.assertIn("names no known project",
                      self.check(f"Contract: {REAL_ID}\n**Contract:** forge.x"))

    def test_an_unknown_namespace_refuses(self) -> None:
        msg = self.check("Contract: forge.gh-argv-parser")
        self.assertIn("names no known project", msg)

    def test_a_malformed_id_refuses(self) -> None:
        self.assertIn("not a contract id", self.check("Contract: Not An Id"))

    # TC3
    def test_no_entry_without_a_search_refuses(self) -> None:
        self.assertIn("Search:", self.check("No index entry applies."))

    def test_an_empty_search_refuses(self) -> None:
        self.assertIn("Search:", self.check("No index entry applies.\nSearch:   "))

    def test_bulleted_indented_and_backticked_forms_pass(self) -> None:
        self.assertIsNone(self.check(f"- Contract: `{REAL_ID}`"))
        self.assertIsNone(self.check(f"  1. contract: {REAL_ID}"))
        self.assertIsNone(self.check("* No index entry applies\n  - Search: `rg x`"))

    def test_a_search_line_alone_refuses(self) -> None:
        self.assertIsNotNone(self.check("Search: `git grep -n x`"))

    def test_an_unreadable_index_refuses(self) -> None:
        bad = self.tmp / "contracts.toml"
        bad.write_text("not = [valid")
        msg = self.check(f"Contract: {REAL_ID}", index=bad)
        self.assertIn("unreadable", msg)


if __name__ == "__main__":
    unittest.main()
