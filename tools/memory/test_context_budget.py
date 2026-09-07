#!/usr/bin/env python3
"""Tests for `context_budget.py` (harmonic-forge#497).

Every test builds its own repo tree under a temp `HOME`. That is not
fastidiousness: harmonic-forge#500's own tests had to be made hermetic in a
follow-up PR (#505) after they read the operator's real machine state and
turned CI red on `main`. The same mistake is cheaper to not make twice.

The property under test is the *membership* of the measured surface, not the
byte total. A total is trivially correct once the set is right, and asserting
one would pin the tests to the operator's current directive sizes.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import context_budget as cb  # noqa: E402


class SurfaceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.repo = self.root / "repo"
        (self.home / ".claude").mkdir(parents=True)
        self.repo.mkdir()
        self._env = mock.patch.dict(
            os.environ, {"CLAUDE_CONFIG_DIR": str(self.home / ".claude")})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._tmp.cleanup)

    def write(self, rel: str, text: str) -> Path:
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def user_md(self, text: str) -> Path:
        path = self.home / ".claude" / "CLAUDE.md"
        path.write_text(text, encoding="utf-8")
        return path

    def categories(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for category, path, _size in cb.surface(self.repo):
            out.setdefault(category, []).append(Path(path).name)
        return out


class MembershipTests(SurfaceTestBase):
    def test_the_standing_surface_is_counted(self) -> None:
        self.user_md("user directives\n")
        self.write("CLAUDE.md", "project directives\n")
        self.write(".claude/rules/unscoped.md", "always injected\n")
        cats = self.categories()
        self.assertEqual(cats["user CLAUDE.md"], ["CLAUDE.md"])
        self.assertEqual(cats["project CLAUDE.md"], ["CLAUDE.md"])
        self.assertEqual(cats["unscoped rule"], ["unscoped.md"])

    def test_windsurfrules_is_not_counted(self) -> None:
        """It is named as canonical in HRSE2's CLAUDE.md but the harness does
        not inject it — measured, not assumed. At 16 KB it was 18% of the
        reported total and the third item the over-budget report told the
        operator to cut, for zero actual saving."""
        self.write("CLAUDE.md", "project\n")
        self.write(".windsurfrules", "x" * 5000)
        self.assertNotIn("rules file", self.categories())
        self.assertEqual(sum(s for _c, _p, s in cb.surface(self.repo)), 8)

    def test_globs_frontmatter_is_still_always_loaded(self) -> None:
        """`globs:` reads like a scope key and is not one. A rules file
        carrying it is injected at session start regardless of what file is
        open, so excluding it under-reports — the failure mode this module
        exists to avoid."""
        self.write(".claude/rules/globbed.md",
                   "---\nglobs: 'backend/**/*.py'\n---\nbody\n")
        self.assertIn("globbed.md", self.categories()["unscoped rule"])

    def test_path_scoped_rules_are_excluded(self) -> None:
        """They are injected on a file match, not at session start. Counting
        them would inflate the figure with text most sessions never load, and
        the budget would then be tuned against a number nothing pays."""
        self.write(".claude/rules/always.md", "no frontmatter, always loaded\n")
        for key in ("appliesTo", "paths", "scope"):
            with self.subTest(key=key):
                self.write(f".claude/rules/{key}.md",
                           f"---\n{key}: 'backend/**/*.py'\n---\nbody\n")
        names = self.categories()["unscoped rule"]
        self.assertEqual(names, ["always.md"])

    def test_a_scope_word_in_prose_does_not_exclude_a_rule(self) -> None:
        """`_is_path_scoped` reads the frontmatter only. A rule file that
        *discusses* scoping is still always-loaded, and dropping it would
        under-report the very number this tool exists to report."""
        self.write(".claude/rules/prose.md",
                   "# Rules\n\nSome rules use `globs:` to declare a scope. "
                   "This one does not.\n")
        self.assertIn("prose.md", self.categories()["unscoped rule"])

    def test_a_scope_key_below_the_frontmatter_block_is_prose(self) -> None:
        body = "---\nname: r\n---\n" + ("filler line\n" * 200) + "paths: '**/*'\n"
        self.write(".claude/rules/late.md", body)
        self.assertIn("late.md", self.categories()["unscoped rule"])

    def test_a_long_frontmatter_block_still_reaches_its_scope_key(self) -> None:
        """The scan window was 600 bytes and failed silently in the wrong
        direction: a path-scoped file whose frontmatter ran past the cap never
        reached its own `paths:` and was counted as always-loaded."""
        long_desc = "d" * 2000
        self.write(".claude/rules/verbose.md",
                   f"---\ndescription: \"{long_desc}\"\npaths:\n  - 'backend/**'\n---\nbody\n")
        self.assertNotIn("unscoped rule", self.categories())

    def test_an_unclosed_frontmatter_block_counts_as_unscoped(self) -> None:
        """No closing delimiter within the window means the classification is
        unknown. Over-reporting shows up in the table; under-reporting does
        not, so unknown resolves to counted."""
        self.write(".claude/rules/broken.md", "---\npaths: 'x'\nbody with no close\n")
        self.assertIn("broken.md", self.categories()["unscoped rule"])

    def test_the_memory_index_is_counted(self) -> None:
        """~20% of the measured total for HRSE2, and an explicit item in the
        issue's scope. Every other category was pinned; this one was not, so
        deleting the row left the suite green."""
        store = self.home / "memory-store"
        store.mkdir(parents=True)
        (store / "MEMORY.md").write_text("x" * 777, encoding="utf-8")
        with mock.patch.object(cb, "resolve_store", return_value=store):
            cats: dict[str, list[str]] = {}
            for category, path, _size in cb.surface(self.repo):
                cats.setdefault(category, []).append(Path(path).name)
            self.assertEqual(cats["memory index"], ["MEMORY.md"])
            self.assertEqual(sum(s for _c, _p, s in cb.surface(self.repo)), 777)

    def test_at_imports_are_resolved_relative_to_the_importer(self) -> None:
        self.write("docs/imported.md", "imported body\n")
        self.write("CLAUDE.md", "top\n@docs/imported.md\n")
        self.assertEqual(self.categories()["project @import"], ["imported.md"])

    def test_a_dangling_import_is_skipped_not_raised(self) -> None:
        self.write("CLAUDE.md", "top\n@docs/nope.md\n")
        self.assertNotIn("project @import", self.categories())

    def test_an_at_sign_mid_line_is_not_an_import(self) -> None:
        """`@` inside prose (an email, a mention) is not import syntax."""
        self.write("docs/imported.md", "x\n")
        self.write("CLAUDE.md", "mail marc@vital-harmony.com and @docs/imported.md\n")
        self.assertNotIn("project @import", self.categories())

    def test_absent_files_contribute_nothing(self) -> None:
        """An empty repo must report an empty surface, not raise."""
        self.assertEqual(cb.surface(self.repo), [])


class ReportingTests(SurfaceTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.write("CLAUDE.md", "x" * 1000)

    def test_summary_line_states_over_or_ok(self) -> None:
        self.assertTrue(cb.summary_line(self.repo, budget=100).endswith("OVER"))
        self.assertTrue(cb.summary_line(self.repo, budget=10_000).endswith("ok"))

    def test_gate_exits_one_only_over_budget(self) -> None:
        argv = ["--repo", str(self.repo), "--summary"]
        self.assertEqual(cb.main(argv + ["--gate", "--budget", "100"]), 1)
        self.assertEqual(cb.main(argv + ["--gate", "--budget", "10000"]), 0)

    def test_without_gate_over_budget_still_exits_zero(self) -> None:
        """`mise run hygiene` calls this report-only. If being over budget
        exited 1 there, the sweep would fail for text no commit touched."""
        self.assertEqual(
            cb.main(["--repo", str(self.repo), "--summary", "--budget", "100"]), 0)

    def test_json_is_parseable_and_totals_agree(self) -> None:
        import io

        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            cb.main(["--repo", str(self.repo), "--json"])
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["total_bytes"],
                         sum(f["bytes"] for f in payload["files"]))
        self.assertEqual(payload["total_bytes"], 1000)

    def test_report_names_the_largest_contributors_when_over(self) -> None:
        text, total = cb.report(self.repo, budget=100)
        self.assertIn("OVER BUDGET", text)
        self.assertIn("Largest contributors:", text)
        self.assertIn("CLAUDE.md", text)
        self.assertEqual(total, 1000)


if __name__ == "__main__":
    unittest.main()
