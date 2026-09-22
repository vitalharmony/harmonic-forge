#!/usr/bin/env python3
"""Focused tests for Lane 1 handoff validation."""

import importlib.util
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
    def test_template_with_real_content_is_accepted(self) -> None:
        body = "\n".join(
            f"### {heading}\nA documented value." for heading in post.HANDOFF_HEADINGS
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
            f"### {heading}\nA documented value." for heading in post.HANDOFF_HEADINGS[:-1]
        )
        with self.assertRaises(SystemExit):
            post.validate_handoff(body, requires_preflight=False)

    def test_live_handoff_cannot_say_none_for_preflight(self) -> None:
        body = "\n".join(
            f"### {heading}\n{'none' if heading == 'Pre-Flight Preconditions' else 'A documented value.'}"
            for heading in post.HANDOFF_HEADINGS
        )
        with self.assertRaises(SystemExit):
            post.validate_handoff(body, requires_preflight=True)


if __name__ == "__main__":
    unittest.main()
