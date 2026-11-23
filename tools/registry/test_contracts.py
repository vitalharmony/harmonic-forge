#!/usr/bin/env python3
"""The contract index (harmonic-forge#1501): validation, freshness and cross-repo resolution."""
from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


class Links(unittest.TestCase):
    def _problems_with(self, **fields):
        entries = _entries()
        entries[0].update(fields)
        return C.problems(entries)

    def test_a_missing_owner_file_fails(self):
        self.assertTrue(self._problems_with(owner="tools/hooks/nope.py: x"))

    def test_an_empty_absolute_or_escaping_path_fails(self):
        for ref in (":89", "/etc/hostname", "../outside.py"):
            with self.subTest(ref=ref):
                self.assertTrue(self._problems_with(example_caller=ref))

    def test_an_existing_file_outside_the_root_is_refused_as_outside(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            (Path(tmp) / "outside.py").write_text("x = 1\n", encoding="utf-8")
            self.assertIn("resolves outside the repository", C.link_problem("../outside.py", root, "file"))

    def test_a_line_past_the_end_of_the_file_fails(self):
        self.assertTrue(any("names line 9999" in p
                            for p in self._problems_with(example_caller="tools/hooks/close_gate.py:9999")))

    def test_a_directory_in_a_file_field_and_a_file_in_scan_roots_fail(self):
        self.assertTrue(self._problems_with(guard=["tools/hooks"]))
        self.assertTrue(self._problems_with(scan_roots=["tools/hooks/gh_argv.py"]))


class PassTwoFixes(unittest.TestCase):
    def test_a_single_contract_table_is_refused_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = Path(tmp) / "contracts.toml"
            index.write_text('[contract]\nid = "harmonic-forge.x"\n', encoding="utf-8")
            with self.assertRaises(SystemExit) as raised:
                C.load(index)
            self.assertIn("array of tables", str(raised.exception))

    def test_an_owner_symbol_that_is_not_defined_fails(self):
        entries = _entries()
        entries[0]["owner"] = "tools/hooks/gh_argv.py: parse, no_such_symbol"
        self.assertTrue(any("no_such_symbol" in p for p in C.problems(entries)))

    def test_the_checked_in_owner_symbols_are_all_defined(self):
        self.assertEqual(C.link_problem(_entries()[0]["owner"], C.FORGE_ROOT, "file"), None)

    def test_a_malformed_or_invalid_foreign_index_is_unmeasured(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = Path(tmp) / "contracts.toml"
            for text in ('[contract]\nid = "hrse.x"\n', '[[contract]]\nid = "hrse.x"\n'):
                with self.subTest(text=text):
                    index.write_text(text, encoding="utf-8")
                    self.assertEqual(C.resolve("hrse.x", _entries(), checkout=lambda name: Path(tmp)), C.UNMEASURED)

    def test_resolve_with_check_is_refused(self):
        with self.assertRaises(SystemExit) as raised:
            C.main(["resolve", "harmonic-forge.gh-argv-parser", "--check"])
        self.assertEqual(raised.exception.code, 2)

    def test_the_first_entry_owner_is_gh_argv(self):
        self.assertEqual(C._path_part(_entries()[0]["owner"]), "tools/hooks/gh_argv.py")


class ResolveCommand(unittest.TestCase):
    def _run(self, ids, checkout):
        with mock.patch.object(C, "project_checkout", checkout):
            return C.main(["resolve", *ids])

    def test_an_id_with_a_missing_checkout_exits_unmeasured_not_green(self):
        self.assertEqual(self._run(["hrse.date-contract"], lambda name: None), 3)

    def test_an_own_id_resolves_and_an_unknown_one_fails(self):
        self.assertEqual(self._run(["harmonic-forge.gh-argv-parser"], lambda name: None), 0)
        self.assertEqual(self._run(["harmonic-forge.no-such"], lambda name: None), 1)

    def test_no_ids_is_never_green(self):
        self.assertEqual(self._run([], lambda name: None), 1)


class Freshness(unittest.TestCase):
    def test_a_crlf_doc_fails_the_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "CONTRACTS.md"
            doc.write_bytes(C.render(_entries()).replace("\n", "\r\n").encode("utf-8"))
            self.assertEqual(C.main(["--check", "--doc", str(doc)]), 1)

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
            (Path(tmp) / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
            (Path(tmp) / "contracts.toml").write_text(
                '[[contract]]\nid = "hrse.date-contract"\nscope = "s"\nowner = "a.py: f"\nmodules = ["a.py"]\n'
                'authority = "a.py"\nexample_caller = "a.py:1"\nguard = ["a.py"]\nexceptions = "none"\n'
                'scan_roots = ["."]\nblind_spots = "none"\nstatus = "canonical"\n', encoding="utf-8")
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
