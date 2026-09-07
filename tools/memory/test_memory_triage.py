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


class RankingTests(unittest.TestCase):
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
        short texts, not evidence."""
        loaded = mt.load_rules(RULES)
        idf = mt.build_idf(loaded)
        rule = next(r for r in loaded if r.rule_id == "R-9001")
        few = set(list(rule.words)[:mt.MIN_SHARED_WORDS - 1])
        self.assertEqual(mt._overlap(few, rule.words, idf), 0.0)

    def test_at_most_the_configured_number_of_candidates(self) -> None:
        for row in rows().values():
            self.assertLessEqual(len(row.candidates), mt.CANDIDATES)


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
                           "promoted", "target", "evidence", "candidates"})

    def test_actionable_classes_are_rendered_first(self) -> None:
        """FOLD rows are the only ones that produce work. Ordering them after
        the 64-row LOCAL/STATE tail is how a report gets skimmed and dropped."""
        text = self._run(self._fixture_argv())
        self.assertLess(text.index("## FOLD+HOOK"), text.index("## LOCAL"))
        self.assertLess(text.index("## FOLD ("), text.index("## STATE"))

    def test_the_filing_bar_is_applied_to_fold_rows(self) -> None:
        text = self._run(self._fixture_argv())
        self.assertIn("| 4 | file ", text)
        self.assertEqual(mt.FILE_THRESHOLD, 2)

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

    def test_the_module_opens_nothing_for_writing(self) -> None:
        source = (HERE / "memory_triage.py").read_text(encoding="utf-8")
        for forbidden in ('"w"', "'w'", "write_text", "mkdir", "unlink",
                          "subprocess", "os.replace"):
            self.assertNotIn(forbidden, source,
                             f"read-only tool must not reference {forbidden}")


if __name__ == "__main__":
    unittest.main()
