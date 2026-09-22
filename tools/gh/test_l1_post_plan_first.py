#!/usr/bin/env python3
"""a private-repo incident — a handoff DECLARES Plan-First; it is never inferred downstream.

`lane_state.py` used to decide with a whole-body `re.compile(r"Plan-First")`
search. It matched a private-repo incident's handoff, whose own sentence is *"this does not
need Plan-First. Implement directly."*

But the deeper reason no body parser can be right: R-0244 has three triggers
and only the first is in the text. Trigger 2 asks whether the implementation's
own operation mutates git state or live data; trigger 3 is a sentence in
operator chat. Both are knowable to the author at post time and to nobody
afterwards — so the derivation belongs here, and the field has to be a hard
requirement or it silently falls back to the legacy path forever.
"""
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


l1_post = load("l1_post")


class PlanFirstDeclarationTests(unittest.TestCase):
    def test_a_missing_declaration_is_a_hard_refusal(self):
        """L1-confirmed: matching `validate_tier_set`'s precedent. An optional
        field is one every author skips, and then `lane_state` falls back to
        the legacy path forever — the drift this design exists to close."""
        with self.assertRaises(SystemExit):
            l1_post.validate_plan_first_declared(None)

    def test_both_declared_values_pass(self):
        for value in ("true", "false"):
            with self.subTest(value=value):
                l1_post.validate_plan_first_declared(value)  # must not raise

    def test_an_unrecognised_value_is_refused(self):
        """`--plan-first` is a choices= argument, so argparse catches this
        first — but the validator must not be the weaker of the two, or a
        programmatic caller could route around it."""
        for value in ("", "yes", "TRUE ", "maybe"):
            with self.subTest(value=value):
                with self.assertRaises(SystemExit):
                    l1_post.validate_plan_first_declared(value)

    def test_the_refusal_names_all_three_triggers(self):
        """An error that says only "declare it" teaches nothing. Triggers 2
        and 3 are the ones an author will not think of, because neither is
        visible in the handoff they just wrote."""
        with self.assertRaises(SystemExit) as caught:
            l1_post.validate_plan_first_declared(None)
        message = str(caught.exception) + getattr(caught.exception, "args", ("",))[0]
        for fragment in ("Delegated Judgment Calls", "mutates git state", "Plan-first #N"):
            self.assertIn(fragment, message)


class FooterFieldTests(unittest.TestCase):
    """The field has to reach the posted comment, not merely be validated."""

    def _footer(self, kind, plan_first):
        captured = {}

        def fake_comment_body(repo, issue, body):
            captured["body"] = body
            return "https://example/1", 1

        (original_comment, original_checks, original_world,
         original_receipt, original_pr) = (
            l1_post.comment_body, l1_post.static_checks,
            l1_post.world_checks, l1_post.write_receipt, l1_post.require_open_pr)
        l1_post.comment_body = fake_comment_body
        l1_post.static_checks = lambda sha, branch: ["body-validation"]
        l1_post.world_checks = lambda *a, **k: ([], [])
        # a private-repo incident: not this file's own concern (Plan-First field placement),
        # so stubbed satisfied exactly like static_checks/world_checks above.
        l1_post.require_open_pr = lambda *a, **k: (["pr-open"], [])
        l1_post.write_receipt = lambda record: captured.setdefault("receipt", record)
        try:
            l1_post.post_kind("o/r", 1, kind, "body", "abc", "br", plan_first=plan_first)
        finally:
            (l1_post.comment_body, l1_post.static_checks,
             l1_post.world_checks, l1_post.write_receipt, l1_post.require_open_pr) = (
                original_comment, original_checks, original_world,
                original_receipt, original_pr)
        return captured

    def test_a_handoff_footer_carries_the_declaration(self):
        captured = self._footer("handoff", True)
        self.assertIn("plan-first=true;", captured["body"])
        self.assertIn("kind=handoff;", captured["body"])

    def test_a_false_declaration_is_written_not_omitted(self):
        """Omitting the field for `false` would make "declared not Plan-First"
        and "never declared" indistinguishable — which is the whole tri-state
        this issue introduces, collapsed back to two."""
        captured = self._footer("handoff", False)
        self.assertIn("plan-first=false;", captured["body"])

    def test_non_handoff_kinds_carry_no_plan_first_field(self):
        """A sweep or an AE has no Plan-First to declare; stamping one would
        make the field meaningless where it appears."""
        for kind in ("ready-for-l3", "sweep", "ae"):
            with self.subTest(kind=kind):
                self.assertNotIn("plan-first=", self._footer(kind, None)["body"])

    def test_the_receipt_records_the_declaration_too(self):
        """The footer is on the comment, which can be edited; the receipt is
        the local durable record, and a private-repo incident's whole lesson is that the
        two must not share one point of failure."""
        self.assertIs(self._footer("handoff", True)["receipt"]["plan_first"], True)

    def test_the_footer_still_carries_every_pre_existing_field(self):
        body = self._footer("handoff", True)["body"]
        for fragment in ("l1-post v1;", "sha=abc;", "body-sha256=", "checks="):
            self.assertIn(fragment, body)


if __name__ == "__main__":
    unittest.main()
