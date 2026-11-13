#!/usr/bin/env python3
"""harmonic-forge#1342: a Lane 3 spec carries a `Run:` fenced block on every
case and, under Write tier W or P, one header `Graph:` line."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
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
    def setUp(self) -> None:
        # No adapter manifest: a fresh empty git repo as the cwd.
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def adapter(self, commands: list[str]) -> None:
        (self.repo / ".claude").mkdir(exist_ok=True)
        (self.repo / ".claude" / "gate-adapter.json").write_text(
            json.dumps({"spec_graph": {"provision_commands": commands}}), encoding="utf-8")

    def problems(self, body: str) -> list[str]:
        return m.spec_structure_problems(body, self.repo)

    def ok(self, body: str) -> None:
        self.assertEqual(self.problems(body), [])

    def refused(self, body: str, *needles: str) -> str:
        problems = self.problems(body)
        self.assertTrue(problems, "expected a refusal")
        text = "; ".join(problems)
        for needle in needles:
            self.assertIn(needle, text)
        return text


class RunBlocks(Base):
    def test_ac1_every_missing_case_is_named(self) -> None:
        text = self.refused(spec("R", None, [RUN, "no command here\n", "nor here\n"]), "TC2", "TC3")
        self.assertNotIn("TC1", text)

    def test_ac2_inline_backticks_never_count(self) -> None:
        self.refused(spec("R", None, ["Run: `match_owner`\n"]), "TC1")
        self.refused(spec("R", None, ["Calls `match_owner` and checks the result.\n"]), "TC1")

    def test_run_line_must_be_followed_by_the_fence(self) -> None:
        self.refused(spec("R", None, ["Run:\nsome prose first\n```\nx\n```\n"]), "TC1")

    def test_a_tilde_fence_counts(self) -> None:
        self.ok(spec("R", None, ["Run:\n~~~\nx\n~~~\n"]))


class TierP(Base):
    def test_ac3_missing_none_and_parts(self) -> None:
        self.refused(spec("P", None, [RUN]), "Graph:")
        self.refused(spec("P", "Graph: none\n", [RUN]), "refuses `Graph: none`")
        self.refused(spec("P", "Graph: none — read only\n", [RUN]), "refuses `Graph: none`")
        self.refused(spec("P", "Graph: `python3 scripts/graph_fixture.py up`\n", [RUN]), "bolt://")
        self.refused(spec("P", "Graph: bolt://localhost:7699\n", [RUN]), "provisioning command")

    def test_conforming_p_passes(self) -> None:
        self.ok(spec("P", GRAPH, [RUN]))


class TierW(Base):
    def test_ac7_minimal_conforming_w_passes(self) -> None:
        self.ok(spec("W", GRAPH, [RUN, RUN]))

    def test_ac4_missing_or_reasonless(self) -> None:
        self.refused(spec("W", None, [RUN]), "Graph:")
        self.refused(spec("W", "Graph: none\n", [RUN]), "reason")

    def test_ac4_none_with_reason_and_graph_free_runs_passes(self) -> None:
        self.ok(spec("W", "Graph: none — every case reads posted artifacts\n", [RUN]))
        self.ok(spec("W", "Graph: none - every case reads posted artifacts\n", [RUN]))

    def test_ac4_none_with_bolt_in_a_run_block_is_refused(self) -> None:
        run = "Run:\n```\ncypher-shell -a bolt://localhost:7699 'RETURN 1'\n```\n"
        self.refused(spec("W", "Graph: none — reads only\n", [run]), "names a graph")

    def test_ac4_none_with_a_declared_command_in_a_run_block_is_refused(self) -> None:
        self.adapter(["scripts/graph_fixture.py"])
        run = "Run:\n```\npython3 scripts/graph_fixture.py up\n```\n"
        self.refused(spec("W", "Graph: none — reads only\n", [run]), "names a graph")

    def test_ac10_colon_is_not_a_separator(self) -> None:
        self.refused(spec("W", "Graph: none: reads only\n", [RUN]), "reason")

    def test_two_header_lines_are_refused(self) -> None:
        self.refused(spec("W", GRAPH + GRAPH, [RUN]), "exactly one")


class Adapter(Base):
    def test_ac5_unlisted_command_is_refused(self) -> None:
        self.adapter(["scripts/graph_fixture.py"])
        self.refused(spec("W", "Graph: `make graph` bolt://localhost:7699\n", [RUN]),
                     "no declared provisioning command")
        self.ok(spec("W", GRAPH, [RUN]))

    def test_ac5_absent_key_accepts_any_command_but_needs_the_uri(self) -> None:
        self.ok(spec("W", "Graph: `make graph` bolt://localhost:7699\n", [RUN]))
        self.refused(spec("W", "Graph: `make graph`\n", [RUN]), "bolt://")


class TierR(Base):
    def test_ac6_r_accepts_none_or_no_line(self) -> None:
        self.ok(spec("R", None, [RUN]))
        self.ok(spec("R", "Graph: none\n", [RUN]))


class FencesAndFooter(Base):
    def test_ac11_a_fenced_example_graph_line_is_ignored(self) -> None:
        example = "```\nGraph: none\n```\n"
        self.ok(spec("W", example + GRAPH, [RUN]))
        self.refused(spec("W", example, [RUN]), "Graph:")

    def test_ac11_a_fenced_example_tc_heading_is_not_a_case(self) -> None:
        self.ok(spec("R", "```\n### TC9 — example\n```\n", [RUN]))

    def test_ac11_footer_neither_satisfies_nor_breaks(self) -> None:
        footer = "\n\n<!-- l1-post v1; kind=spec; Graph: none — x; Run: -->\n"
        self.refused(spec("W", None, [RUN]) + footer, "Graph:")
        self.refused(spec("R", None, ["no command\n"]) + footer, "TC1")
        self.ok(spec("W", GRAPH, [RUN]) + footer)


class FailClosed(Base):
    """sticky-wicket PATCH (harmonic-forge#1342 pass 1): every not-found or
    unparseable branch refuses with a named reason."""

    def test_unclosed_fence_is_refused_naming_its_line(self) -> None:
        body = spec("R", None, [RUN + "```\nnever closed\n", "prose only\n"])
        self.refused(body, "unclosed code fence opened at line")

    def test_zero_tc_headings_is_refused(self) -> None:
        body = "## Lane 3 Test Spec — H1\n\nWrite tier: R\n**AE by name:** none\n\nNo cases.\n"
        self.refused(body, "no `### TC<n>` case heading")

    def test_case_label_outside_tc_form_is_refused(self) -> None:
        for label in ("#### TC2 — x", "**TC2** — x", "- TC2 — x", "### TC-2 — x", "1. TC2 — x"):
            with self.subTest(label=label):
                self.refused(spec("R", None, [RUN]) + f"\n{label}\n{RUN}", "outside `### TC<n>` form")

    def test_unreadable_manifest_is_refused(self) -> None:
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude" / "gate-adapter.json").write_text("{not json", encoding="utf-8")
        self.refused(spec("W", GRAPH, [RUN]), "adapter manifest is unusable")

    def test_malformed_spec_graph_is_refused(self) -> None:
        for value in ([], "x", {"provision_commands": "scripts/graph_fixture.py"},
                      {"provision_commands": ["ok", ""]}, {"provision_commands": [1]}):
            with self.subTest(value=value):
                (self.repo / ".claude").mkdir(exist_ok=True)
                (self.repo / ".claude" / "gate-adapter.json").write_text(
                    json.dumps({"spec_graph": value}), encoding="utf-8")
                self.refused(spec("W", GRAPH, [RUN]), "malformed spec_graph")

    def test_manifest_without_spec_graph_accepts_any_command(self) -> None:
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude" / "gate-adapter.json").write_text("{}", encoding="utf-8")
        self.ok(spec("W", "Graph: `make graph` bolt://localhost:7699\n", [RUN]))
        self.refused(spec("W", "Graph: `make graph`\n", [RUN]), "bolt://")

    def test_cwd_repo_other_than_target_is_refused(self) -> None:
        subprocess.run(["git", "-C", str(self.repo), "remote", "add", "origin",
                        "https://github.com/vitalharmony/harmonic-forge.git"], check=True)
        self.assertEqual(m.spec_repo_cwd("vitalharmony/harmonic-forge", self.repo).resolve(),
                         self.repo.resolve())
        with self.assertRaises(SystemExit):
            m.spec_repo_cwd("vitalharmony/hrse", self.repo)

    def test_cwd_without_origin_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            m.spec_repo_cwd("vitalharmony/hrse", self.repo)


class RealSpecs(Base):
    def test_ac8_the_four_real_hrse_specs_are_refused(self) -> None:
        self.assertEqual(len(REAL_SPECS), 4)
        for path in REAL_SPECS:
            with self.subTest(spec=path.name), self.assertRaises(SystemExit):
                m.require_spec_structure(path.read_text(encoding="utf-8"), self.repo)


class Wiring(unittest.TestCase):
    def test_the_spec_branch_calls_the_lint_on_final(self) -> None:
        source = (HERE / "post_lane_discussion.py").read_text(encoding="utf-8")
        self.assertIn("        require_ae_by_name(final)\n"
                      "        require_spec_structure(final, spec_repo_cwd(args.repo))\n", source)


class Schema(unittest.TestCase):
    def test_ac9_schema_accepts_spec_graph_and_rejects_unknown_subkeys(self) -> None:
        schema = json.loads((HERE.parents[1] / "schemas" / "gate-adapter.schema.json")
                            .read_text(encoding="utf-8"))
        entry = schema["properties"]["spec_graph"]
        self.assertIs(entry["additionalProperties"], False)
        self.assertEqual(set(entry["properties"]), {"provision_commands"})
        try:
            import jsonschema  # noqa: PLC0415
        except ImportError:
            return
        jsonschema.validate({"spec_graph": {"provision_commands": ["x"]}}, schema)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate({"spec_graph": {"other": 1}}, schema)


if __name__ == "__main__":
    unittest.main()
