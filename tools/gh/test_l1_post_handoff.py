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
