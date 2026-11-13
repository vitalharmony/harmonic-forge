#!/usr/bin/env python3
"""harmonic-forge#1342: a Lane 3 spec carries a `Run:` fenced block on every
`### TC<n>` case and, under Write tier W or P, one header `Graph:` line.
Narrowed by the sticky-wicket REFORGE (2026-11-13): no adapter allowlist, a
graph is any bolt/neo4j URI, and only the template's own shape is checked."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import post_lane_discussion as m  # noqa: E402

FIXTURES = HERE / "fixtures" / "spec_structure"
REAL_SPECS = sorted(FIXTURES.glob("hrse_spec_*.md"))

RUN = "Run:\n```\npython3 -m pytest -q tests/x.py\n```\n"
GRAPH = "Graph: `python3 scripts/graph_fixture.py up` bolt://localhost:7699\n"


def spec(tier: str, graph: str | None, cases: list[str]) -> str:
    head = f"## Lane 3 Test Spec — H1\n\nWrite tier: {tier}\n**AE by name:** none\n"
    if graph is not None:
        head += graph
    return head + "".join(f"\n### TC{i + 1} — case\n{c}" for i, c in enumerate(cases))


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


class AC1RunBlocks(Base):
    def test_every_missing_case_is_named(self) -> None:
        text = self.refused(spec("R", None, [RUN, "no command here\n", "nor here\n"]), "TC2", "TC3")
        self.assertNotIn("TC1", text)

    def test_zero_tc_headings_is_refused(self) -> None:
        body = "## Lane 3 Test Spec — H1\n\nWrite tier: R\n**AE by name:** none\n\nNo cases.\n"
        self.refused(body, "no `### TC<n>` case heading")

    def test_unclosed_fence_is_refused_naming_its_line(self) -> None:
        body = spec("R", None, [RUN + "```\nnever closed\n", "prose only\n"])
        self.refused(body, "unclosed code fence opened at line")

    def test_an_empty_run_fence_is_refused(self) -> None:
        self.refused(spec("R", None, ["Run:\n```\n```\n"]), "TC1")
        self.refused(spec("R", None, ["Run:\n```bash\n   \n\n```\n"]), "TC1")

    def test_run_line_must_be_followed_by_the_fence(self) -> None:
        self.refused(spec("R", None, ["Run:\nsome prose first\n```\nx\n```\n"]), "TC1")

    def test_list_and_bold_prefixes_pass(self) -> None:
        for run in ("- Run:\n```\nx\n```\n", "* Run:\n```\nx\n```\n",
                    "**Run:**\n```\nx\n```\n", "- **Run:**\n```\nx\n```\n",
                    "Run:\n~~~\nx\n~~~\n"):
            with self.subTest(run=run):
                self.ok(spec("R", None, [run]))

    def test_numbered_run_with_an_indented_fence_passes(self) -> None:
        self.ok(spec("R", None, ["1. Run:\n   ```\n   python3 -m pytest -q\n   ```\n"]))


class AC2InlineBackticks(Base):
    def test_inline_backticks_never_count(self) -> None:
        self.refused(spec("R", None, ["Run: `match_owner`\n"]), "TC1")
        self.refused(spec("R", None, ["Calls `match_owner` and checks the result.\n"]), "TC1")


class AC3TierP(Base):
    def test_missing_none_and_parts(self) -> None:
        self.refused(spec("P", None, [RUN]), "needs one header `Graph:` line")
        self.refused(spec("P", "Graph: none\n", [RUN]), "refuses `Graph: none`")
        self.refused(spec("P", "Graph: none — read only\n", [RUN]), "refuses `Graph: none`")
        self.refused(spec("P", "Graph: `python3 scripts/graph_fixture.py up`\n", [RUN]),
                     "no graph URI")
        self.refused(spec("P", "Graph: bolt://localhost:7699\n", [RUN]), "no provisioning command")

    def test_conforming_p_passes(self) -> None:
        self.ok(spec("P", GRAPH, [RUN]))

    def test_every_uri_scheme_counts(self) -> None:
        for scheme in ("bolt", "bolt+s", "bolt+ssc", "neo4j", "neo4j+s", "neo4j+ssc"):
            with self.subTest(scheme=scheme):
                self.ok(spec("P", f"Graph: `make graph` {scheme}://db:7687\n", [RUN]))


class AC4TierW(Base):
    def test_missing_or_reasonless(self) -> None:
        self.refused(spec("W", None, [RUN]), "needs one header `Graph:` line")
        self.refused(spec("W", "Graph: none\n", [RUN]), "reason")

    def test_missing_command_or_uri(self) -> None:
        self.refused(spec("W", "Graph: `make graph`\n", [RUN]), "no graph URI")
        self.refused(spec("W", "Graph: neo4j://db:7687\n", [RUN]), "no provisioning command")

    def test_none_with_reason_and_graph_free_runs_passes(self) -> None:
        self.ok(spec("W", "Graph: none — every case reads posted artifacts\n", [RUN]))

    def test_none_with_a_graph_uri_in_a_run_block_is_refused(self) -> None:
        for uri in ("bolt://localhost:7699", "bolt+s://db:7687", "bolt+ssc://db:7687",
                    "neo4j://localhost:7687", "neo4j+s://db:7687", "neo4j+ssc://db:7687"):
            with self.subTest(uri=uri):
                run = f"Run:\n```\ncypher-shell -a {uri} 'RETURN 1'\n```\n"
                self.refused(spec("W", "Graph: none — reads only\n", [run]), "names a graph")

    def test_two_header_lines_are_refused(self) -> None:
        self.refused(spec("W", GRAPH + GRAPH, [RUN]), "exactly one")


class AC5OtherCaseLabels(Base):
    def test_a_cross_reference_bullet_passes(self) -> None:
        self.ok(spec("R", "- TC1 covers AC1\n- TC2 covers AC2\n", [RUN, RUN]))

    def test_a_tc_summary_heading_passes(self) -> None:
        self.ok(spec("R", None, [RUN]) + "\n## TC summary\n| TC | AC |\n|---|---|\n| TC1 | AC1 |\n")


class AC6TierR(Base):
    def test_r_accepts_none_or_no_line(self) -> None:
        self.ok(spec("R", None, [RUN]))
        self.ok(spec("R", "Graph: none\n", [RUN]))


class AC7MinimalW(Base):
    def test_minimal_conforming_w_passes(self) -> None:
        self.ok(spec("W", GRAPH, [RUN, RUN]))


class AC8RealSpecs(unittest.TestCase):
    def test_the_four_real_hrse_specs_are_refused(self) -> None:
        self.assertEqual(len(REAL_SPECS), 4)
        for path in REAL_SPECS:
            with self.subTest(spec=path.name), self.assertRaises(SystemExit):
                m.require_spec_structure(path.read_text(encoding="utf-8"))


class AC10Separator(Base):
    def test_spaced_hyphen_is_a_separator(self) -> None:
        self.ok(spec("W", "Graph: none - every case reads posted artifacts\n", [RUN]))

    def test_colon_is_not_a_separator(self) -> None:
        self.refused(spec("W", "Graph: none: reads only\n", [RUN]), "reason")


class AC11FencesAndFooter(Base):
    def test_a_fenced_example_graph_line_is_ignored(self) -> None:
        example = "```\nGraph: none\n```\n"
        self.ok(spec("W", example + GRAPH, [RUN]))
        self.refused(spec("W", example, [RUN]), "needs one header `Graph:` line")

    def test_a_fenced_example_tc_heading_is_not_a_case(self) -> None:
        self.ok(spec("R", "```\n### TC9 — example\n```\n", [RUN]))

    def test_footer_neither_satisfies_nor_breaks(self) -> None:
        footer = "\n\n<!-- l1-post v1; kind=spec; Graph: none — x; Run: -->\n"
        self.refused(spec("W", None, [RUN]) + footer, "needs one header `Graph:` line")
        self.refused(spec("R", None, ["no command\n"]) + footer, "TC1")
        self.ok(spec("W", GRAPH, [RUN]) + footer)


class Wiring(unittest.TestCase):
    def test_the_spec_branch_calls_the_lint_on_final(self) -> None:
        source = (HERE / "post_lane_discussion.py").read_text(encoding="utf-8")
        self.assertIn("        require_ae_by_name(final)\n"
                      "        require_spec_structure(final)\n", source)

    def test_no_adapter_allowlist_remains(self) -> None:
        self.assertFalse(hasattr(m, "_provision_commands"))
        self.assertFalse(hasattr(m, "spec_repo_cwd"))


if __name__ == "__main__":
    unittest.main()
