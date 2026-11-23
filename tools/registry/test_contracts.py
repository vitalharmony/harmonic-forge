#!/usr/bin/env python3
"""The contract index (harmonic-forge#1501): validation, freshness and cross-repo resolution."""
from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import contracts as C  # noqa: E402


def _entries() -> list[dict]:
    return copy.deepcopy(C.load())


class Validate(unittest.TestCase):
    def test_the_checked_in_index_is_valid(self):
        self.assertEqual(C.problems(_entries()), [])

    def test_a_duplicate_id_fails(self):
        entries = _entries()
        entries.append(copy.deepcopy(entries[0]))
        self.assertTrue(any("duplicate id" in p for p in C.problems(entries)))

    def test_a_module_path_that_does_not_exist_fails(self):
        entries = _entries()
        entries[0]["modules"] = ["tools/hooks/no_such_module.py"]
        self.assertTrue(any("no_such_module.py" in p for p in C.problems(entries)))

    def test_a_scan_of_zero_entries_fails(self):
        self.assertTrue(C.problems([]))

    def test_an_id_outside_this_repo_namespace_fails(self):
        entries = _entries()
        entries[0]["id"] = "hrse.gh-argv-parser"
        self.assertTrue(any("must start with" in p for p in C.problems(entries)))

    def test_a_missing_field_and_an_unknown_status_fail(self):
        entries = _entries()
        del entries[0]["guard"]
        entries[0]["status"] = "maybe"
        found = C.problems(entries)
        self.assertTrue(any("`guard`" in p for p in found))
        self.assertTrue(any("`status`" in p for p in found))

    def test_the_first_entry_is_the_gh_argv_contract(self):
        first = _entries()[0]
        self.assertEqual(first["id"], "harmonic-forge.gh-argv-parser")
        self.assertIn("tools/hooks/gh_argv.py", first["modules"])
        self.assertTrue((C.FORGE_ROOT / "tools/hooks/gh_argv.py").is_file())


class Freshness(unittest.TestCase):
    def test_the_checked_in_doc_is_a_fresh_render(self):
        self.assertEqual(C.main(["--check"]), 0)

    def test_a_hand_edited_doc_fails_the_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "CONTRACTS.md"
            doc.write_text(C.render(_entries()) + "hand edit\n", encoding="utf-8")
            self.assertEqual(C.main(["--check", "--doc", str(doc)]), 1)

    def test_a_missing_doc_fails_the_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(C.main(["--check", "--doc", str(Path(tmp) / "absent.md")]), 1)


class CrossRepo(unittest.TestCase):
    def test_an_own_id_resolves_and_an_unknown_own_id_does_not(self):
        entries = _entries()
        self.assertEqual(C.resolve("harmonic-forge.gh-argv-parser", entries), C.RESOLVED)
        self.assertEqual(C.resolve("harmonic-forge.no-such", entries), C.UNRESOLVED)

    def test_an_id_whose_checkout_is_missing_is_unmeasured(self):
        self.assertEqual(C.resolve("hrse.date-contract", _entries(), checkout=lambda name: None), C.UNMEASURED)

    def test_an_id_whose_checkout_has_no_index_is_unmeasured(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                C.resolve("hrse.date-contract", _entries(), checkout=lambda name: Path(tmp)), C.UNMEASURED)

    def test_an_id_in_another_checkout_resolves_from_its_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "contracts.toml").write_text('[[contract]]\nid = "hrse.date-contract"\n', encoding="utf-8")
            resolve = lambda cid: C.resolve(cid, _entries(), checkout=lambda name: Path(tmp))  # noqa: E731
            self.assertEqual(resolve("hrse.date-contract"), C.RESOLVED)
            self.assertEqual(resolve("hrse.other"), C.UNRESOLVED)

    def test_project_checkout_reads_the_manifest_and_reports_a_missing_path_as_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "projects.toml"
            manifest.write_text(
                f'[[project]]\nname = "hrse"\npath = "{tmp}/absent"\n'
                f'[[project]]\nname = "here"\npath = "{tmp}"\n', encoding="utf-8")
            self.assertIsNone(C.project_checkout("hrse", manifest))
            self.assertEqual(C.project_checkout("here", manifest), Path(tmp))
            self.assertIsNone(C.project_checkout("unknown", manifest))


if __name__ == "__main__":
    unittest.main()
