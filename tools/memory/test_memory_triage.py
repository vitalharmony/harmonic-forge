#!/usr/bin/env python3
"""Tests for `memory_triage.py` (harmonic-forge#495).

Every test runs against `testdata/triage/`, never the live store or the live
rule files. That is AC2's requirement and also harmonic-forge#500's lesson: its
tests read operator machine state and turned CI red on `main` (fixed in #505).
A classifier pinned to a corpus that grows every week cannot have a stable
regression test.

The fixture is small and its rules are deliberately about unrelated subjects,
so the ranker has something to be right or wrong about. A fixture that mirrored
the real corpus — 261 rules on one protocol — would pin these tests to
vocabulary statistics nobody controls.
"""
from __future__ import annotations

import io
import os
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import memory_triage as mt  # noqa: E402

FIXTURE = HERE / "testdata" / "triage"
STORE = FIXTURE / "store"
RULES = [FIXTURE / "rules"]


def rows() -> dict[str, mt.Row]:
    result = mt.triage(STORE, mt.load_rules(RULES), RULES)
    return {r.name: r for r in result}


class FixtureIntegrityTests(unittest.TestCase):
    """The fixture must actually exercise what the tests below claim.

    hrse#1631 and harmonic-forge#500 both shipped subtests that passed without
    touching the feature they named. These assertions are the guard against a
    third: if the fixture ever stops containing one of the six classes, the
    tests below would still pass while proving nothing.
    """

    def test_the_fixture_covers_every_class(self) -> None:
        verdicts = {r.verdict for r in rows().values()}
        self.assertEqual(verdicts, set(mt._ORDER))

    def test_the_fixture_rules_actually_load(self) -> None:
        """A zero-rule corpus silently degrades every verdict — the exact bug
        this fixture caught on its first run (`testdata` was skipped even when
        named explicitly). A rule count of 0 must never read as healthy."""
        loaded = mt.load_rules(RULES)
        self.assertEqual({r.rule_id for r in loaded}, {"R-9001", "R-9002", "R-9003"})


class ClassificationTests(unittest.TestCase):
    def test_declared_type_decides_state_and_local_before_any_text_analysis(self) -> None:
        by_name = rows()
        self.assertEqual(by_name["project_release"].verdict, "STATE")
        self.assertEqual(by_name["reference_runtime"].verdict, "LOCAL")

    def test_a_reference_memory_is_local_even_when_it_matches_a_rule_closely(self) -> None:
        """`reference_runtime` is near-verbatim R-9003. It is still not a
        lesson, and letting the overlap score reach it would file promotion
        dispositions against operator context."""
        row = rows()["reference_runtime"]
        self.assertEqual(row.verdict, "LOCAL")
        self.assertEqual(row.candidates, [],
                         "type-decided rows must not carry rule candidates")

    def test_a_resolving_promoted_marker_is_the_only_dup(self) -> None:
        by_name = rows()
        dups = [r.name for r in by_name.values() if r.verdict == "DUP"]
        self.assertEqual(dups, ["feedback_promoted"])
        self.assertEqual(by_name["feedback_promoted"].target, "R-9001")

    def test_a_promoted_marker_naming_no_rule_is_stale_not_dup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "feedback_x.md"
            path.write_text(
                "---\nname: feedback_x\ndescription: d\npromoted: R-9999\n"
                "metadata:\n  type: feedback\n---\n\nbody\n", encoding="utf-8")
            loaded = mt.load_rules(RULES)
            row = mt.classify(path, loaded, RULES, mt.build_idf(loaded))
        self.assertEqual(row.verdict, "STALE")
        self.assertIn("R-9999", row.evidence)

    def test_stale_is_evidenced_by_a_path_that_exists_nowhere(self) -> None:
        row = rows()["feedback_stale"]
        self.assertEqual(row.verdict, "STALE")
        self.assertIn("nonexistent_helper_xyz.py", row.evidence)

    def test_a_path_cited_by_basename_alone_resolves(self) -> None:
        """`_stale_evidence` falls back to `rglob(basename)` because memories
        cite `memory_lint.py`, not `tools/memory/memory_lint.py`. That branch
        decides 88% of the class: dropping it takes the live STALE count from
        2 to 39 — all false "delete" dispositions — and the suite still passed,
        because the only negative case wrote the cited file at the exact path
        the `exists()` branch already handles."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "deep" / "nested"
            nested.mkdir(parents=True)
            (nested / "buried_tool.py").write_text("x", encoding="utf-8")
            self.assertEqual(mt._stale_evidence("see `buried_tool.py`", [root]), "")
            self.assertIn("gone_tool.py",
                          mt._stale_evidence("see `gone_tool.py`", [root]))

    def test_a_cited_sibling_memory_is_not_stale(self) -> None:
        """A memory citing another memory by filename is citing something that
        exists. Before the store was added as a search root this put a live
        row in the STALE class."""
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            (store / "reference_runtime.md").write_text("x", encoding="utf-8")
            path = store / "feedback_y.md"
            path.write_text(
                "---\nname: feedback_y\ndescription: d\nmetadata:\n  type: feedback\n"
                "---\n\nSee `reference_runtime.md` for the details.\n",
                encoding="utf-8")
            loaded = mt.load_rules(RULES)
            row = mt.classify(path, loaded, RULES, mt.build_idf(loaded))
        self.assertNotEqual(row.verdict, "STALE")

    def test_hookability_splits_fold_from_fold_hook(self) -> None:
        by_name = rows()
        self.assertEqual(by_name["feedback_worktree"].verdict, "FOLD+HOOK")
        self.assertEqual(by_name["feedback_scheduling"].verdict, "FOLD")

    def test_the_hook_split_is_driven_by_the_trigger_text_not_the_filename(self) -> None:
        """Mutating only the body must flip the verdict. Without this the two
        assertions above would pass on any implementation that happened to
        sort those two files differently."""
        loaded = mt.load_rules(RULES)
        idf = mt.build_idf(loaded)
        head = ("---\nname: feedback_z\ndescription: d\nmetadata:\n"
                "  type: feedback\n---\n\n")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "feedback_z.md"
            path.write_text(head + "Say thank you in the closing line.\n",
                            encoding="utf-8")
            self.assertEqual(mt.classify(path, loaded, RULES, idf).verdict, "FOLD")
            path.write_text(head + "Never write to the main branch.\n",
                            encoding="utf-8")
            self.assertEqual(mt.classify(path, loaded, RULES, idf).verdict,
                             "FOLD+HOOK")


class StaleDemotionTests(unittest.TestCase):
    """A stale citation must not delete a recurring lesson (preclose finding)."""

    def _row(self, instances: int) -> mt.Row:
        loaded = mt.load_rules(RULES)
        idf = mt.build_idf(loaded)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "feedback_cite.md"
            path.write_text(
                f"---\nname: feedback_cite\ndescription: d\ninstances: {instances}\n"
                "metadata:\n  type: feedback\n---\n\n"
                "Verify live behavior. See `absolutely_gone_file.py`.\n",
                encoding="utf-8")
            return mt.classify(path, loaded, RULES, idf)

    def test_a_one_off_with_a_dead_citation_is_stale(self) -> None:
        self.assertEqual(self._row(1).verdict, "STALE")

    def test_a_recurring_lesson_keeps_its_fold_verdict(self) -> None:
        """`feedback_verify_live_not_source` — 3 recurrences, the strongest in
        the set — was routed to "verify, then delete" over a renamed component
        while the lesson itself was entirely current."""
        row = self._row(3)
        self.assertIn(row.verdict, ("FOLD", "FOLD+HOOK"))
        self.assertIn("absolutely_gone_file.py", row.stale_note)

    def test_the_dead_citation_still_reaches_the_report(self) -> None:
        """Demoting it to a note is only safe if the reader still sees it."""
        self.assertIn("absolutely_gone_file.py", mt.render([self._row(3)]))


class AuditComparisonTests(unittest.TestCase):
    """AC2, restated: the audit is a measured REFERENCE, not an expected value."""

    def test_the_vendored_audit_parses(self) -> None:
        audit = mt.parse_audit(mt.AUDIT_FIXTURE)
        self.assertGreater(len(audit), 200)
        self.assertEqual(set(audit.values()) - set(mt._ORDER), set())

    def test_the_audit_carries_every_class(self) -> None:
        """A parser that silently dropped most rows would report high
        agreement over the handful it kept."""
        self.assertEqual(set(mt.parse_audit(mt.AUDIT_FIXTURE).values()),
                         set(mt._ORDER))

    def test_the_report_states_agreement_and_labels_it_a_reference(self) -> None:
        audit = {"a": "FOLD", "b": "DUP"}
        rows = [mt.Row(name="a", path=Path("a"), kind="feedback", instances=2,
                       first_seen="", promoted="", verdict="FOLD", evidence=""),
                mt.Row(name="b", path=Path("b"), kind="feedback", instances=2,
                       first_seen="", promoted="", verdict="FOLD", evidence="")]
        text = mt.audit_report(rows, audit)
        self.assertIn("2 files in both, 1 agree (50%)", text)
        self.assertIn("DUP → FOLD", text)
        self.assertIn("REFERENCE, not a target", text)

    def test_no_overlap_says_so_rather_than_dividing_by_zero(self) -> None:
        self.assertIn("nothing to compare", mt.audit_report([], {"x": "FOLD"}))


class RankingTests(unittest.TestCase):
    def test_a_fold_target_is_a_filename_not_a_rule_id(self) -> None:
        """AC3 says every FOLD row names a target that exists on disk.
        Changing `row.candidates[0][2]` to `[1]` makes every target an `R-` id
        and the AC false for all 140 live rows — and passed, because the only
        target assertion in the suite was on the DUP row, where an `R-` id is
        correct."""
        for name in ("feedback_worktree", "feedback_scheduling"):
            with self.subTest(memory=name):
                target = rows()[name].target
                self.assertTrue(target.endswith(".md"), target)
                self.assertFalse(target.startswith("R-"), target)
                self.assertTrue((FIXTURE / "rules" / target).is_file(), target)

    def test_each_fold_ranks_its_own_subject_rule_first(self) -> None:
        by_name = rows()
        self.assertEqual(by_name["feedback_worktree"].candidates[0][1], "R-9001")
        self.assertEqual(by_name["feedback_scheduling"].candidates[0][1], "R-9002")

    def test_idf_weighting_is_what_produces_that_ordering(self) -> None:
        """Unweighted containment ranks both memories against whichever rule is
        longest, because the shared protocol vocabulary dominates. Pinning the
        ordering without pinning its cause would let a regression to plain
        containment pass."""
        loaded = mt.load_rules(RULES)
        flat = {w: 1.0 for w in mt.build_idf(loaded)}
        weighted = mt.build_idf(loaded)
        self.assertNotEqual(
            [round(v, 3) for v in sorted(weighted.values())],
            [round(v, 3) for v in sorted(flat.values())])
        mem = mt._words("worktree disposable per-issue branch switch shared lane")
        rule = next(r for r in loaded if r.rule_id == "R-9001")
        self.assertGreater(mt._overlap(mem, rule.words, weighted), 0.0)

    def test_too_few_shared_words_scores_zero(self) -> None:
        """A high score off two or three coincidental words is an artifact of
        short texts, not evidence.

        The word counts here are LITERAL, not derived from
        `MIN_SHARED_WORDS`. Reading the constant to build the input made the
        expectation move with the change under test, so every value except 0
        passed — including 8, which alters two live rows' reported target.
        """
        loaded = mt.load_rules(RULES)
        idf = mt.build_idf(loaded)
        rule = next(r for r in loaded if r.rule_id == "R-9001")
        words = sorted(rule.words)
        self.assertGreaterEqual(len(words), 5, "fixture rule too small to test the floor")
        self.assertEqual(mt.MIN_SHARED_WORDS, 4)
        self.assertEqual(mt._overlap(set(words[:3]), rule.words, idf), 0.0)
        self.assertGreater(mt._overlap(set(words[:4]), rule.words, idf), 0.0)

    def test_at_most_three_candidates(self) -> None:
        """Literal 3, not `mt.CANDIDATES` — reading the constant meant setting
        it to 1 or 10 passed while every row's candidate list changed."""
        self.assertEqual(mt.CANDIDATES, 3)
        for row in rows().values():
            self.assertLessEqual(len(row.candidates), 3)


class OutputTests(unittest.TestCase):
    def _run(self, argv: list[str]) -> str:
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            self.assertEqual(mt.main(argv), 0)
        return out.getvalue()

    def _fixture_argv(self, *extra: str) -> list[str]:
        return ["--store", str(STORE), "--repo-root", str(RULES[0]), *extra]

    def test_counts_are_json_and_total_agrees(self) -> None:
        payload = json.loads(self._run(self._fixture_argv("--counts")))
        self.assertEqual(payload["total"], sum(payload["counts"].values()))
        self.assertEqual(payload["total"], 6)

    def test_counts_are_the_frozen_fixture_baseline(self) -> None:
        """AC2. One of each class — a shape a re-run must reproduce exactly,
        not within a tolerance: on a fixed store with a fixed rule corpus there
        is nothing left for a tolerance to absorb."""
        payload = json.loads(self._run(self._fixture_argv("--counts")))
        self.assertEqual(payload["counts"], {
            "FOLD+HOOK": 1, "FOLD": 1, "DUP": 1,
            "STALE": 1, "LOCAL": 1, "STATE": 1})

    def test_json_rows_carry_every_field_the_skill_reads(self) -> None:
        payload = json.loads(self._run(self._fixture_argv("--json")))
        self.assertEqual(len(payload), 6)
        for row in payload:
            self.assertEqual(
                set(row), {"name", "verdict", "type", "instances", "first_seen",
                           "promoted", "target", "evidence", "candidates",
                           "stale_note"})

    def test_actionable_classes_are_rendered_first(self) -> None:
        """FOLD rows are the only ones that produce work. Ordering them after
        the 64-row LOCAL/STATE tail is how a report gets skimmed and dropped."""
        text = self._run(self._fixture_argv())
        self.assertLess(text.index("## FOLD+HOOK"), text.index("## LOCAL"))
        self.assertLess(text.index("## FOLD ("), text.index("## STATE"))

    def test_the_filing_bar_boundary_is_inclusive(self) -> None:
        """`>=` vs `>` at the threshold. The old test asserted only the
        instances-4 row, which is "file" under either comparison — so
        regressing to `>` passed, and on the live store that silently
        downgrades every instances==2 row to "do not file", emptying the class
        the bar exists to identify."""
        text = self._run(self._fixture_argv())
        self.assertIn("| `feedback_scheduling` | 2 | file ", text)   # == threshold
        self.assertIn("| `feedback_worktree` | 4 | file ", text)     # above

    def test_below_the_threshold_is_batch_not_file(self) -> None:
        """The "batch" side of the branch was never asserted at all."""
        loaded = mt.load_rules(RULES)
        idf = mt.build_idf(loaded)
        head = ("---\nname: feedback_once\ndescription: d\ninstances: 1\n"
                "metadata:\n  type: feedback\n---\n\nA one-off note.\n")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "feedback_once.md"
            path.write_text(head, encoding="utf-8")
            row = mt.classify(path, loaded, RULES, idf)
        self.assertEqual(row.instances, 1)
        self.assertLess(row.instances, mt.FILE_THRESHOLD)
        text = mt.render([row])
        self.assertIn("| batch |", text)
        self.assertNotIn("| file |", text)

    def test_a_missing_store_exits_two_not_zero(self) -> None:
        """Exit 2 distinguishes "could not run" from "ran and found nothing".
        A bare non-zero check would let a deleted store read as a finding."""
        with mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(mt.main(["--store", "/nonexistent/store"]), 2)


class ReadOnlyTests(unittest.TestCase):
    """AC4: zero writes outside the report path."""

    def test_a_full_run_leaves_the_fixture_byte_identical(self) -> None:
        before = {p: p.read_bytes() for p in sorted(FIXTURE.rglob("*")) if p.is_file()}
        with mock.patch.object(sys, "stdout", io.StringIO()):
            mt.main(["--store", str(STORE), "--repo-root", str(RULES[0])])
        after = {p: p.read_bytes() for p in sorted(FIXTURE.rglob("*")) if p.is_file()}
        self.assertEqual(before, after)

    def test_a_full_run_writes_nothing_anywhere_under_home(self) -> None:
        """Behavioral, not a source grep.

        The grep this replaces checked for `"w"` and `write_text` but not `'a'`,
        `write_bytes`, `open(p, mode)`, `shutil.copy`, `Path.touch` or
        `os.system` — appending to a file under $HOME during `main()` passed
        both AC4 tests and left the file on disk. It also made the string
        `subprocess` unusable anywhere in the module, comments included.
        """
        with tempfile.TemporaryDirectory() as home:
            fake = Path(home)
            (fake / "marker").write_text("x", encoding="utf-8")
            before = self._snapshot(fake)
            with mock.patch.dict(os.environ, {"HOME": str(fake)}), \
                    mock.patch.object(Path, "home", staticmethod(lambda: fake)), \
                    mock.patch.object(sys, "stdout", io.StringIO()):
                mt.main(["--store", str(STORE), "--repo-root", str(RULES[0])])
            self.assertEqual(self._snapshot(fake), before)

    @staticmethod
    def _snapshot(root: Path) -> dict[str, bytes]:
        return {str(p.relative_to(root)): p.read_bytes()
                for p in sorted(root.rglob("*")) if p.is_file()}

    def test_the_ast_shows_no_write_mode_open(self) -> None:
        """The structural half, done properly: every `open()`/`Path.open()` in
        the module is inspected for a mode argument rather than the source
        being string-searched."""
        import ast

        tree = ast.parse((HERE / "memory_triage.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if name not in {"open", "write_text", "write_bytes", "touch", "mkdir"}:
                continue
            self.assertEqual(name, "open", f"{name}() is a write path")
            modes = [a.value for a in node.args[1:] if isinstance(a, ast.Constant)]
            modes += [k.value.value for k in node.keywords
                      if k.arg == "mode" and isinstance(k.value, ast.Constant)]
            for mode in modes:
                self.assertNotRegex(str(mode), r"[wax+]", f"open(mode={mode!r})")


if __name__ == "__main__":
    unittest.main()
