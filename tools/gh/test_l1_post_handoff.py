#!/usr/bin/env python3
"""Focused tests for Lane 1 handoff validation."""

import importlib.util
import re
from pathlib import Path
import unittest

ROOT = Path(__file__).parent


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


post = load("l1_post")


class HandoffTests(unittest.TestCase):

    def _body(self, consumers: str) -> str:
        return "\n".join(
            f"### {heading}\n{consumers if heading == 'Consumers and Equivalents' else 'A documented value, per `git grep -n value` (l1_post.py:94), verified-live.'}"
            for heading in post.HANDOFF_HEADINGS
        )

    FILLER = "A documented value, per `git grep -n value` (l1_post.py:94), verified-live."

    def _with_trace(self, trace: str | None) -> str:
        """Every heading real, `Scenario Trace` set to `trace` (None drops it)."""
        return "\n".join(
            f"### {h}\n{trace if h == 'Scenario Trace' else self.FILLER}"
            for h in post.HANDOFF_HEADINGS if h != "Scenario Trace" or trace is not None)

    def test_scenario_trace_is_a_required_heading_after_root_cause(self) -> None:
        """harmonic-forge#920 AC1: absent, and the post is refused."""
        headings = post.HANDOFF_HEADINGS
        self.assertEqual(headings.index("Scenario Trace"),
                         headings.index("Root Cause / Entry Point") + 1)
        with self.assertRaises(SystemExit) as caught:
            post.validate_handoff(self._with_trace(None), requires_preflight=False)
        self.assertIn("Scenario Trace", str(caught.exception))

    def test_scenario_trace_placeholder_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            post.validate_handoff(self._with_trace("{the issue's own example, hop by hop}"),
                                  requires_preflight=False)

    def test_scenario_trace_without_a_file_line_is_refused(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            post.validate_handoff(
                self._with_trace("1. The user opens the screen, verified-live by reading it."),
                requires_preflight=False)
        self.assertIn("file:line", str(caught.exception))

    def test_scenario_trace_without_verified_live_is_refused(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            post.validate_handoff(
                self._with_trace("1. The user opens the screen: lib/app_router.dart:158."),
                requires_preflight=False)
        self.assertIn("verified-live", str(caught.exception))

    def test_the_templates_own_unfilled_scenario_trace_is_refused_as_a_placeholder(self) -> None:
        """The template carries guidance after its placeholder line, so the section
        is more than a single {...} span: the placeholder phrase must still catch it."""
        template = (Path(__file__).resolve().parent.parent.parent / "templates"
                    / "lane1-handoff.md").read_text(encoding="utf-8")
        section = re.search(r"(?ms)^### Scenario Trace\s*$\n(.*?)(?=^### )", template)
        self.assertIsNotNone(section, "templates/lane1-handoff.md has no Scenario Trace section")
        with self.assertRaises(SystemExit) as caught:
            post.validate_handoff(self._with_trace(section.group(1).strip()),
                                  requires_preflight=False)
        self.assertIn("template placeholder: Scenario Trace", str(caught.exception))

    def test_the_template_and_the_validator_list_the_same_headings_in_the_same_order(self) -> None:
        """Parity (harmonic-forge#920 pitch-inspection fixture run): a heading added to
        HANDOFF_HEADINGS without its templated section makes the first real post refuse."""
        template = (Path(__file__).resolve().parent.parent.parent / "templates"
                    / "lane1-handoff.md").read_text(encoding="utf-8")
        in_template = re.findall(r"(?m)^### (.+?)\s*$", template)
        self.assertEqual([h for h in in_template if h in post.HANDOFF_HEADINGS],
                         post.HANDOFF_HEADINGS)

    def _refused(self, trace: str) -> str:
        with self.assertRaises(SystemExit) as caught:
            post.validate_handoff(self._with_trace(trace), requires_preflight=False)
        return str(caught.exception)

    def test_host_port_and_version_strings_are_not_file_references(self) -> None:
        """harmonic-forge#920 pass 1: `[\\w./-]+\\.\\w+:\\d+` accepted all three."""
        for trace in ("1. Operator opens http://127.0.0.1:8002/docs, verified-live (curl).",
                      "1. https://app.leasepal.example.com:8443/quote verified-live",
                      "1. Pinned neo4j v5.26:3 image, verified-live."):
            self.assertIn("file:line", self._refused(trace), trace)

    def test_extensionless_tracked_entry_points_are_file_references(self) -> None:
        for ref in ("tools/lane/lane1:42", "Makefile:12", "Dockerfile:30", ".githooks/pre-commit:5",
                    "lib/ui/lp_menu_dock.dart:9-30", "l1_post.py:94"):
            post.validate_handoff(self._with_trace(f"1. Entry {ref}, verified-live (read)."),
                                  requires_preflight=False)

    def test_the_templates_guidance_never_supplies_the_verified_live_marker(self) -> None:
        """Pass 1, five lenses: keep the guidance, replace only the placeholder line with
        one unverified hop. The guidance is a comment now and is also not evidence."""
        template = (Path(__file__).resolve().parent.parent.parent / "templates"
                    / "lane1-handoff.md").read_text(encoding="utf-8")
        section = re.search(r"(?ms)^### Scenario Trace\s*$\n(.*?)(?=^### )", template).group(1)
        filled = re.sub(r"(?m)^\{the issue's own example[^\n]*\}$",
                        "1. Renter taps Quote -> lib/app_router.dart:158 -> LpAppDock.", section)
        self.assertIn("verified-live", filled)  # the guidance text is still there
        self.assertIn("verified-live", self._refused(filled))
        # the same guidance as bare, uncommented prose must not count either
        bare = filled.replace("<!--", "").replace("-->", "")
        self.assertIn("verified-live", self._refused(bare))

    def test_a_hop_needs_the_file_line_and_the_marker_on_the_same_line(self) -> None:
        message = self._refused("1. Route /quote -> lib/app_router.dart:158 (read).\n"
                                "2. It was verified-live, per the above.")
        self.assertIn("BOTH", message)

    def test_a_quoted_heading_in_a_fence_is_not_the_section(self) -> None:
        body = ("```\n### Scenario Trace\n1. quoted: x.py:1 verified-live\n```\n\n"
                + self._with_trace(None))
        with self.assertRaises(SystemExit) as caught:
            post.validate_handoff(body, requires_preflight=False)
        self.assertIn("Scenario Trace", str(caught.exception))
        quoted_then_real = ("```\n### Scenario Trace\n{TBD}\n```\n\n"
                            + self._with_trace("1. lib/app_router.dart:158, verified-live (read)."))
        post.validate_handoff(quoted_then_real, requires_preflight=False)

    def test_a_heading_line_inside_pasted_output_does_not_truncate_the_trace(self) -> None:
        trace = ("1. Run `mise run check`, output below.\n```\n### Summary\nok\n```\n"
                 "2. Entry lib/app_router.dart:158, verified-live (read).")
        post.validate_handoff(self._with_trace(trace), requires_preflight=False)

    def test_hops_inside_a_code_fence_do_not_count(self) -> None:
        self.assertIn("file:line", self._refused("```\nlib/app_router.dart:158 verified-live\n```"))

    def test_a_complete_scenario_trace_posts_and_other_headings_are_unchanged(self) -> None:
        post.validate_handoff(
            self._with_trace("1. Route /quote -> lib/app_router.dart:158, **verified-live** "
                             "(read 2026-10-06)."), requires_preflight=False)
        body = self._with_trace("1. lib/app_router.dart:158 verified-live.").replace(
            "### Ambiguity Gate\n" + self.FILLER, "### Ambiguity Gate\n{unfilled}")
        with self.assertRaises(SystemExit):
            post.validate_handoff(body, requires_preflight=False)

    def test_consumers_heading_is_required(self) -> None:
        """harmonic-forge#838 AC4."""
        self.assertIn("Consumers and Equivalents", post.HANDOFF_HEADINGS)
        body = "\n".join(f"### {h}\nA documented value, per `git grep -n value` (l1_post.py:94), verified-live." for h in post.HANDOFF_HEADINGS
                         if h != "Consumers and Equivalents")
        with self.assertRaises(SystemExit):
            post.validate_handoff(body, requires_preflight=False)

    def test_consumers_placeholder_is_refused(self) -> None:
        """AC4 is structural: the template's own placeholder never posts."""
        with self.assertRaises(SystemExit):
            post.validate_handoff(self._body("{none | for each contract this handoff changes}"),
                                  requires_preflight=False)

    def test_consumers_prose_is_not_pattern_matched(self) -> None:
        """Sticky-wicket PATCH (F838): whether a search ran is not in the text,
        so l1_post checks only structure and pitch-inspection checks the rest."""
        post.validate_handoff(self._body("none, per the search recorded in the review"),
                              requires_preflight=False)

    def test_none_with_its_search_is_accepted(self) -> None:
        post.validate_handoff(self._body("none: `git grep -n HANDOFF_HEADINGS` finds only l1_post.py"),
                              requires_preflight=False)

    def test_none_elsewhere_still_needs_no_search(self) -> None:
        """The rule is scoped to this one heading (plan review change 4)."""
        body = self._body("`HANDOFF_HEADINGS`: l1_post.py (`git grep -n HANDOFF_HEADINGS`)").replace(
            "### Design Alternatives Considered\nA documented value, per `git grep -n value` (l1_post.py:94), verified-live.",
            "### Design Alternatives Considered\nnone")
        post.validate_handoff(body, requires_preflight=False)
    def test_template_with_real_content_is_accepted(self) -> None:
        body = "\n".join(
            f"### {heading}\nA documented value, per `git grep -n value` (l1_post.py:94), verified-live." for heading in post.HANDOFF_HEADINGS
        )
        post.validate_handoff(body, requires_preflight=False)

    def test_code_snippets_with_braces_are_substantive(self) -> None:
        for snippet in (
            ".button { color: red; }",
            "const config = { enabled: true };",
            'ALLOWED_LABELS = {"bug", "feature"}',
        ):
            with self.subTest(snippet=snippet):
                self.assertTrue(post.is_substantive(snippet))

    def test_literal_template_instruction_is_not_substantive(self) -> None:
        placeholder = (
            "{none | list each plausible design that was weighed and why it was rejected "
            "in favor of the chosen one}"
        )
        self.assertFalse(post.is_substantive(placeholder))

    def test_issue_number_template_remains_not_substantive(self) -> None:
        self.assertFalse(post.is_substantive("[Issue #N — Short Title]"))

    def test_missing_heading_is_rejected(self) -> None:
        body = "\n".join(
            f"### {heading}\nA documented value, per `git grep -n value` (l1_post.py:94), verified-live." for heading in post.HANDOFF_HEADINGS[:-1]
        )
        with self.assertRaises(SystemExit):
            post.validate_handoff(body, requires_preflight=False)

    def test_live_handoff_cannot_say_none_for_preflight(self) -> None:
        body = "\n".join(
            f"### {heading}\n{'none' if heading == 'Pre-Flight Preconditions' else 'A documented value, per `git grep -n value` (l1_post.py:94), verified-live.'}"
            for heading in post.HANDOFF_HEADINGS
        )
        with self.assertRaises(SystemExit):
            post.validate_handoff(body, requires_preflight=True)


if __name__ == "__main__":
    unittest.main()
