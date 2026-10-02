#!/usr/bin/env python3
"""Focused tests for Lane 1 handoff validation."""

import importlib.util
from pathlib import Path
import unittest
from unittest import mock

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
            f"### {heading}\n{consumers if heading == 'Consumers and Equivalents' else 'A documented value, per `git grep -n value`.'}"
            for heading in post.HANDOFF_HEADINGS
        )

    def test_consumers_heading_is_required(self) -> None:
        """harmonic-forge#838 AC4."""
        self.assertIn("Consumers and Equivalents", post.HANDOFF_HEADINGS)
        body = "\n".join(f"### {h}\nA documented value, per `git grep -n value`." for h in post.HANDOFF_HEADINGS
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
            "### Design Alternatives Considered\nA documented value, per `git grep -n value`.",
            "### Design Alternatives Considered\nnone")
        post.validate_handoff(body, requires_preflight=False)
    def test_template_with_real_content_is_accepted(self) -> None:
        body = "\n".join(
            f"### {heading}\nA documented value, per `git grep -n value`." for heading in post.HANDOFF_HEADINGS
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
            f"### {heading}\nA documented value, per `git grep -n value`." for heading in post.HANDOFF_HEADINGS[:-1]
        )
        with self.assertRaises(SystemExit):
            post.validate_handoff(body, requires_preflight=False)

    def test_live_handoff_cannot_say_none_for_preflight(self) -> None:
        body = "\n".join(
            f"### {heading}\n{'none' if heading == 'Pre-Flight Preconditions' else 'A documented value, per `git grep -n value`.'}"
            for heading in post.HANDOFF_HEADINGS
        )
        with self.assertRaises(SystemExit):
            post.validate_handoff(body, requires_preflight=True)


class MutatesLiveFooterTests(unittest.TestCase):
    """harmonic-forge#851 AC3.5: the handoff footer records `--mutates-live`."""

    def _post(self, mutates_live: bool) -> tuple[str, dict]:
        posted: dict = {}
        def comment(repo, issue, body):
            posted["body"] = body
            return f"https://github.com/{repo}/issues/{issue}#issuecomment-9", 9
        with mock.patch.object(post, "world_checks", return_value=([], [])), \
             mock.patch.object(post, "comment_body", side_effect=comment), \
             mock.patch.object(post, "write_receipt") as receipt, \
             mock.patch.object(post, "_discharge_handoff_owed"), \
             mock.patch.object(post.pitch_receipt, "consume"):
            post.post_kind("vitalharmony/harmonic-forge", 851, "handoff", "## Handoff\n\nbody",
                           "a" * 40, "main", is_handoff_extra_checks=True,
                           plan_first=False, mutates_live=mutates_live)
        return posted["body"], receipt.call_args.args[0]

    def test_mutates_live_true_is_recorded(self) -> None:
        body, receipt = self._post(True)
        self.assertIn("kind=handoff; plan-first=false; mutates-live=true; sha=", body)
        self.assertTrue(receipt["mutates_live"])

    def test_mutates_live_false_is_recorded(self) -> None:
        body, receipt = self._post(False)
        self.assertIn("kind=handoff; plan-first=false; mutates-live=false; sha=", body)
        self.assertFalse(receipt["mutates_live"])

    def test_the_footer_reader_round_trips(self) -> None:
        import _handoff_footer
        for value in (True, False):
            with self.subTest(value=value):
                body, _ = self._post(value)
                self.assertIs(_handoff_footer.newest_handoff_mutates_live([body]), value)


if __name__ == "__main__":
    unittest.main()
