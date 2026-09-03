#!/usr/bin/env python3
"""Tests for the rule registry and its drift check (harmonic-forge#447).

AC5 is asserted in **both** directions — the check must fail on drift and
pass on the unmodified tree. A check only ever exercised on the failing
side can be vacuously strict; one only exercised on the passing side is the
`narrative_budget_check.py` failure this platform already deleted a tool
for.
"""

import tempfile
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_rule_drift as drift  # noqa: E402
import query_rules as query  # noqa: E402

_ROOT = Path(__file__).resolve().parent.parent.parent


def _write(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return path


def _registry(root: Path, rows: str) -> Path:
    return _write(root, "registry.toml", rows)


class SpanExtraction(unittest.TestCase):
    def test_paired_markers_yield_the_span_between_them(self):
        path = _write(Path(self.tmp), "rules/x.md", """
            # Title
            <!-- R-0001 -->
            - Never do the thing.
            <!-- /R-0001 -->
            trailing narrative
        """)
        spans = drift.extract_spans(path)
        self.assertEqual(list(spans), ["R-0001"])
        self.assertIn("Never do the thing.", spans["R-0001"])
        self.assertNotIn("trailing narrative", spans["R-0001"])

    def test_two_spans_in_one_file(self):
        """One bullet may yield several IDs — the classification unit is the
        obligation, not the markup — so multiple spans per file is the
        normal case, not an edge case."""
        path = _write(Path(self.tmp), "rules/x.md", """
            <!-- R-0001 -->
            - First obligation.
            <!-- /R-0001 -->
            <!-- R-0002 -->
            - Second obligation.
            <!-- /R-0002 -->
        """)
        self.assertEqual(sorted(drift.extract_spans(path)), ["R-0001", "R-0002"])

    def test_unclosed_span_is_an_error_not_a_skip(self):
        """A half-annotated span would silently drop a rule out of every
        later count — the counts are the whole point."""
        path = _write(Path(self.tmp), "rules/x.md", """
            <!-- R-0001 -->
            - Never closed.
        """)
        with self.assertRaises(ValueError) as ctx:
            drift.extract_spans(path)
        self.assertIn("never closed", str(ctx.exception).lower())

    def test_mismatched_close_is_an_error(self):
        path = _write(Path(self.tmp), "rules/x.md", """
            <!-- R-0001 -->
            - Thing.
            <!-- /R-0002 -->
        """)
        with self.assertRaises(ValueError):
            drift.extract_spans(path)

    def test_nested_spans_are_rejected(self):
        path = _write(Path(self.tmp), "rules/x.md", """
            <!-- R-0001 -->
            <!-- R-0002 -->
            - Thing.
            <!-- /R-0002 -->
            <!-- /R-0001 -->
        """)
        with self.assertRaises(ValueError):
            drift.extract_spans(path)

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()


class SpanSha(unittest.TestCase):
    def test_reflow_is_not_drift(self):
        """Rewrapping a line or trimming trailing space does not change the
        obligation, and treating it as drift would produce constant false
        failures on a prose corpus."""
        a = drift.span_sha("- Never do   the thing.\n")
        b = drift.span_sha("   - Never do   the thing.   \n\n")
        self.assertEqual(a, b)

    def test_a_word_change_is_drift(self):
        a = drift.span_sha("- Never do the thing.")
        b = drift.span_sha("- Always do the thing.")
        self.assertNotEqual(a, b)


class DriftCheck(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _write(self.root, "rules/x.md", """
            <!-- R-0001 -->
            - Never do the thing.
            <!-- /R-0001 -->
        """)
        self.sha = drift.span_sha("- Never do the thing.")

    def tearDown(self):
        self._tmp.cleanup()

    def _reg(self, **over):
        row = {"id": "R-0001", "file": "rules/x.md", "anchor": "a",
               "statement": "s", "text_sha": self.sha}
        row.update(over)
        return _registry(self.root, textwrap.dedent(f"""
            [[rule]]
            id = "{row['id']}"
            file = "{row['file']}"
            anchor = "{row['anchor']}"
            statement = "{row['statement']}"
            text_sha = "{row['text_sha']}"
            hooks = []
        """))

    def test_passes_on_the_unmodified_tree(self):
        """AC5, the passing direction."""
        self.assertEqual(drift.check(self.root, self._reg()), [])

    def test_fails_when_the_rule_text_changed_without_its_id(self):
        """AC5, the failing direction — the case `statement` alone cannot
        catch, because `statement` is a paraphrase."""
        _write(self.root, "rules/x.md", """
            <!-- R-0001 -->
            - Always do the thing.
            <!-- /R-0001 -->
        """)
        failures = drift.check(self.root, self._reg())
        self.assertEqual(len(failures), 1)
        self.assertIn("span text changed", failures[0])

    def test_fails_when_a_registered_rule_has_no_span(self):
        """Rule deleted or moved without its ID following."""
        _write(self.root, "rules/x.md", "- Never do the thing.\n")
        failures = drift.check(self.root, self._reg())
        self.assertTrue(any("no `<!-- R-0001 -->` span" in f for f in failures))

    def test_fails_when_a_span_has_no_registry_row(self):
        """The other orphan direction — every marker needs a row."""
        _write(self.root, "rules/x.md", """
            <!-- R-0001 -->
            - Never do the thing.
            <!-- /R-0001 -->
            <!-- R-0002 -->
            - An unregistered obligation.
            <!-- /R-0002 -->
        """)
        failures = drift.check(self.root, self._reg())
        self.assertTrue(any("R-0002" in f and "absent from the registry" in f for f in failures))

    def test_detects_duplicate_ids(self):
        """Two branches can each allocate the same ID and each pass their own
        check; they collide at merge. Preventing that needs a central
        allocator, which is worse — so it is detected, loudly."""
        registry = _registry(self.root, textwrap.dedent(f"""
            [[rule]]
            id = "R-0001"
            file = "rules/x.md"
            anchor = "a"
            statement = "s"
            text_sha = "{self.sha}"
            hooks = []

            [[rule]]
            id = "R-0001"
            file = "rules/x.md"
            anchor = "a"
            statement = "a different rule that took the same number"
            text_sha = "{self.sha}"
            hooks = []
        """))
        failures = drift.check(self.root, registry)
        self.assertTrue(any("duplicate ID R-0001" in f for f in failures))

    def test_reports_every_failure_not_just_the_first(self):
        _write(self.root, "rules/x.md", """
            <!-- R-0001 -->
            - Reworded.
            <!-- /R-0001 -->
            <!-- R-0009 -->
            - Unregistered.
            <!-- /R-0009 -->
        """)
        failures = drift.check(self.root, self._reg())
        self.assertGreaterEqual(len(failures), 2)


class LiveRegistry(unittest.TestCase):
    """Against the real annotated corpus, not a fixture."""

    def test_the_shipped_registry_is_clean(self):
        failures = drift.check(_ROOT, Path(__file__).resolve().parent / "registry.toml")
        self.assertEqual(failures, [], f"registry drift: {failures}")

    def test_every_row_has_the_required_fields(self):
        rules = query.load_rules(Path(__file__).resolve().parent / "registry.toml")
        self.assertGreater(len(rules), 0)
        for rule in rules:
            for field in ("id", "file", "anchor", "statement", "text_sha"):
                self.assertIn(field, rule, f"{rule.get('id')} missing {field}")
            self.assertRegex(rule["id"], r"^R-\d{4}$")

    def test_restates_targets_exist(self):
        """AC4's relation is only meaningful if it resolves."""
        rules = query.load_rules(Path(__file__).resolve().parent / "registry.toml")
        ids = {r["id"] for r in rules}
        for rule in rules:
            if rule.get("restates"):
                self.assertIn(rule["restates"], ids,
                              f"{rule['id']} restates {rule['restates']}, which does not exist")

    def test_next_id_allocator_is_past_every_existing_id(self):
        out = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent / "check_rule_drift.py"), "--next-id"],
            capture_output=True, text=True, check=True).stdout.strip()
        rules = query.load_rules(Path(__file__).resolve().parent / "registry.toml")
        highest = max(int(r["id"].split("-")[1]) for r in rules)
        self.assertEqual(out, f"R-{highest + 1:04d}")


class HookInventory(unittest.TestCase):
    def test_the_scan_covers_more_than_one_settings_file(self):
        """The failure this exists to prevent: enumerating hooks from one
        settings.json understates mechanization, which deflates AC4 — the
        number the corpus trim is sized from."""
        self.assertGreater(len(query._WIRING_LOCATIONS), 1)
        self.assertIn("user-global", query._WIRING_LOCATIONS)

    def test_agent_frontmatter_is_scanned(self):
        """`deny_advisory_subagent_gh_writes.py` is wired ONLY in agent
        frontmatter — a settings.json-only scan misses it entirely."""
        found = query._scripts_in_agent_frontmatter(_ROOT)
        self.assertIn("deny_advisory_subagent_gh_writes.py", found)

    def test_enforcement_is_derived_not_stored(self):
        self.assertEqual(query.enforcement_of({"hooks": [{"script": "x"}]}), "hook")
        self.assertEqual(query.enforcement_of({"hooks": []}), "prose")


class UnknownRegistryField(unittest.TestCase):
    """A misspelled field must fail, not be silently ignored.

    `folded_obligations` records obligations that have no ID of their own.
    Typed as `folded_obligation` it would be read by nothing and reported by
    nothing — the rule would drop out of `--folded` and the undercount would
    become invisible again, which is the exact failure the field exists to
    prevent. tomllib accepts any key, so this is the only place it can fail.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        _write(self.root, "rules/x.md", """
            <!-- R-0001 -->
            - A rule.
            <!-- /R-0001 -->
            """)
        self.sha = drift.span_sha("- A rule.")

    def _check(self, extra: str) -> list[str]:
        registry = _registry(self.root, f"""
            [[rule]]
            id = "R-0001"
            file = "rules/x.md"
            anchor = "x"
            statement = "A rule."
            text_sha = "{self.sha}"
            hooks = []
            {extra}
            """)
        return drift.check(self.root, registry)

    def test_known_field_passes(self):
        self.assertEqual(self._check('folded_obligations = ["a second obligation"]'), [])

    def test_misspelled_field_fails(self):
        failures = self._check('folded_obligation = ["a second obligation"]')
        self.assertEqual(len(failures), 1)
        self.assertIn("unrecognized registry field", failures[0])
        self.assertIn("folded_obligation", failures[0])

    def test_every_field_the_live_registry_uses_is_declared(self):
        """Guards the declaration itself: a field added to real rows but not
        to _KNOWN_FIELDS would fail the live registry, and a field removed
        from the set would too. This catches it at the source instead."""
        rules = query.load_rules(_ROOT / "tools" / "rules" / "registry.toml")
        used = {key for rule in rules for key in rule}
        self.assertEqual(used - drift._KNOWN_FIELDS, set())


class FoldedObligations(unittest.TestCase):
    """The known-folded record (operator decision 2026-09-03: accept the
    undercount, make it visible)."""

    def setUp(self) -> None:
        self.rules = query.load_rules(_ROOT / "tools" / "rules" / "registry.toml")

    def test_live_registry_records_the_folded_obligations(self):
        folded = [r for r in self.rules if r.get("folded_obligations")]
        self.assertTrue(folded, "the known-folded obligations must be recorded")
        total = sum(len(r["folded_obligations"]) for r in folded)
        self.assertGreaterEqual(total, len(folded),
                                "a rule may fold more than one obligation")

    def test_folded_obligations_are_non_empty_strings(self):
        for rule in self.rules:
            for obligation in rule.get("folded_obligations", []):
                with self.subTest(rule=rule["id"]):
                    self.assertIsInstance(obligation, str)
                    self.assertTrue(obligation.strip(),
                                    "an empty entry records nothing")

    def test_folded_rules_are_real_annotated_rules(self):
        """A folded obligation hangs off a rule that actually exists — the
        record is worthless if it points at a stale ID."""
        ids = {r["id"] for r in self.rules}
        for rule in self.rules:
            if rule.get("folded_obligations"):
                self.assertIn(rule["id"], ids)


if __name__ == "__main__":
    unittest.main()
