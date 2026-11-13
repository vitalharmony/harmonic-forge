#!/usr/bin/env python3
"""harmonic-forge#1342 (final reforge, operator ruling "2 yes", 2026-11-13): a
Lane 3 spec carries exactly one fenced `lane3-spec` JSON block, parsed
strictly. Markdown is never read for case structure."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import post_lane_discussion as m  # noqa: E402

FIXTURES = HERE / "fixtures" / "spec_structure"
REAL_SPECS = sorted(FIXTURES.glob("hrse_spec_*.md"))

GRAPH = {"provision": "python3 scripts/graph_fixture.py up", "uri": "bolt://localhost:7699"}
NONE = {"none": "every case reads the posted artifacts"}


def case(n: int, run: str = "python3 -m pytest -q tests/x.py") -> dict:
    return {"id": f"TC{n}", "run": run}


def block(obj) -> str:
    text = obj if isinstance(obj, str) else json.dumps(obj)
    return f"```lane3-spec\n{text}\n```\n"


def spec(tier: str, obj, prose: str = "") -> str:
    return (f"## Lane 3 Test Spec — H1\n\nWrite tier: {tier}\n**AE by name:** none\n\n"
            f"{prose}{block(obj)}")


class Base(unittest.TestCase):
    def ok(self, body: str) -> None:
        self.assertEqual(m.spec_structure_problems(body), [])

    def refused(self, body: str, *needles: str) -> str:
        problems = m.spec_structure_problems(body)
        self.assertTrue(problems, "expected a refusal")
        text = "; ".join(problems)
        for needle in needles:
            self.assertIn(needle, text)
        return text


class AC1Block(Base):
    def test_zero_blocks_is_refused(self) -> None:
        self.refused("## Lane 3 Test Spec — H1\n\nWrite tier: R\n\n### TC1 — a\n", "no ```lane3-spec")

    def test_two_blocks_are_refused(self) -> None:
        body = spec("R", {"cases": [case(1)]}) + block({"cases": [case(2)]})
        self.refused(body, "2 ```lane3-spec blocks")

    def test_an_unclosed_block_is_refused(self) -> None:
        body = "Write tier: R\n```lane3-spec\n{\"cases\": []}\n"
        self.refused(body, "never closed")

    def test_invalid_json_is_refused(self) -> None:
        self.refused(spec("R", "{cases: [}"), "not valid JSON")

    def test_an_unknown_key_is_refused(self) -> None:
        self.refused(spec("R", {"cases": [case(1)], "extra": 1}), "unknown keys: extra")

    def test_an_indented_or_commented_opener_is_not_a_block(self) -> None:
        body = ("Write tier: R\n<!-- ```lane3-spec -->\n  ```lane3-spec\n"
                "{\"cases\": [{\"id\": \"TC1\", \"run\": \"x\"}]}\n  ```\n")
        self.refused(body, "no ```lane3-spec")


class AC2Cases(Base):
    def test_empty_cases_are_refused(self) -> None:
        self.refused(spec("R", {"cases": []}), "non-empty list")

    def test_a_blank_run_is_refused_naming_the_case(self) -> None:
        self.refused(spec("R", {"cases": [case(1), case(2, "   ")]}), "TC2", "non-blank command")

    def test_a_bad_id_is_refused(self) -> None:
        for bad in ("Case 1", "TC1a", "TC1\n"):
            with self.subTest(bad=bad):
                self.refused(spec("R", {"cases": [{"id": bad, "run": "x"}]}), "TC<n>")

    def test_a_non_string_id_or_run_is_refused_not_a_crash(self) -> None:
        for case_ in ({"id": 1, "run": "x"}, {"id": None, "run": "x"}, {"id": "TC1", "run": 2}):
            with self.subTest(case=case_):
                self.refused(spec("R", {"cases": [case_]}), "both strings")

    def test_an_extra_case_key_is_refused(self) -> None:
        self.refused(spec("R", {"cases": [{"id": "TC1", "run": "x", "note": "y"}]}), "TC1")

    def test_duplicate_ids_are_refused(self) -> None:
        self.refused(spec("R", {"cases": [case(1), case(1)]}), "duplicate")


class AC3TierP(Base):
    def test_p_refuses_none_and_a_missing_graph(self) -> None:
        self.refused(spec("P", {"graph": NONE, "cases": [case(1)]}), "Write tier P refuses")
        self.refused(spec("P", {"cases": [case(1)]}), "needs a `graph` key")

    def test_p_refuses_a_blank_provision_or_a_non_graph_uri(self) -> None:
        self.refused(spec("P", {"graph": {"provision": " ", "uri": GRAPH["uri"]}, "cases": [case(1)]}),
                     "graph.provision")
        self.refused(spec("P", {"graph": {"provision": "x", "uri": "localhost:7687"}, "cases": [case(1)]}),
                     "graph.uri")

    def test_p_with_a_graph_passes(self) -> None:
        self.ok(spec("P", {"graph": GRAPH, "cases": [case(1)]}))


class AC4TierW(Base):
    def test_w_none_needs_a_reason(self) -> None:
        self.refused(spec("W", {"graph": {"none": " "}, "cases": [case(1)]}), "non-blank reason")

    def test_w_none_refused_when_a_run_names_a_graph_uri(self) -> None:
        for uri in ("bolt://h:7687", "bolt+s://h", "bolt+ssc://h", "neo4j://h", "neo4j+s://h", "neo4j+ssc://h"):
            with self.subTest(uri=uri):
                self.refused(spec("W", {"graph": NONE, "cases": [case(1, f"seed.py --uri {uri}")]}),
                             "names a graph URI")

    def test_w_needs_a_graph_key(self) -> None:
        self.refused(spec("W", {"cases": [case(1)]}), "needs a `graph` key")


class AC5TierR(Base):
    def test_r_accepts_no_graph_or_none(self) -> None:
        self.ok(spec("R", {"cases": [case(1)]}))
        self.ok(spec("R", {"graph": NONE, "cases": [case(1)]}))


class AC6ProseCrossCheck(Base):
    def test_a_prose_tc_heading_missing_from_the_block_is_refused(self) -> None:
        body = spec("R", {"cases": [case(1)]}, prose="### TC1 — a\n\n### TC2 — b\n\n")
        self.refused(body, "`### TC2` has no case")

    def test_other_prose_labels_are_ignored(self) -> None:
        prose = "**TC9 — b**\n- TC8 covers AC1\n### Test 7\n<!-- x --> <!-- y\n\n"
        self.ok(spec("R", {"cases": [case(1)]}, prose=prose))


class AC7Conforming(Base):
    def test_minimal_w_r_and_w_none_pass(self) -> None:
        self.ok(spec("W", {"graph": GRAPH, "cases": [case(1), case(2)]}, prose="### TC1 — a\n### TC2 — b\n"))
        self.ok(spec("R", {"cases": [case(1)]}))
        self.ok(spec("W", {"graph": NONE, "cases": [case(1)]}))


class AC8RealSpecs(Base):
    def test_four_real_hrse_specs_are_each_refused(self) -> None:
        self.assertEqual(len(REAL_SPECS), 4)
        for path in REAL_SPECS:
            with self.subTest(spec=path.name):
                self.refused(path.read_text(encoding="utf-8"), "no ```lane3-spec")


class AC9Template(Base):
    def test_the_template_and_rule_show_a_valid_example_block(self) -> None:
        root = HERE.parent.parent
        for doc in (root / "skills" / "lane3-gate-platform" / "SKILL.md", root / "rules" / "testing-gate.md"):
            with self.subTest(doc=doc.name):
                text = doc.read_text(encoding="utf-8")
                start = text.index("```lane3-spec")
                lines = text[start:].split("\n")
                body = "\n".join(line.strip() for line in lines[: next(
                    i for i in range(1, len(lines)) if lines[i].strip() == "```") + 1])
                # The example block itself parses and passes a Tier R lint.
                self.assertEqual(m.spec_structure_problems("Write tier: R\n" + body + "\n"), [])


class Wiring(Base):
    def test_require_spec_structure_refuses(self) -> None:
        with self.assertRaises(SystemExit):
            m.require_spec_structure("Write tier: R\n")


if __name__ == "__main__":
    unittest.main()
