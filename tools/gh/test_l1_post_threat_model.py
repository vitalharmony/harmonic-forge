#!/usr/bin/env python3
"""Tests for the Tooling Exception Threat model lead field (harmonic-forge#1192)."""
import importlib.util
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))  # l1_post imports its siblings; run from any cwd


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


post = load("l1_post")
TE = {"tooling-exception", "infrastructure"}


def _handoff(threat: str | None) -> str:
    lead = "## Handoff: [Issue #1192 — test]\n\n**Scope:** test scope.\n**Next:** Lane 1 implements.\n"
    if threat is not None:
        lead += f"**Threat model:** {threat}\n"
    return lead + "\n### Issue\n- body\n"


def _ctx(labels=TE, tier="fast", issue=1192):
    ctx = post.LeadCtx("vitalharmony/harmonic-forge", issue, labels)
    ctx._tier.append(tier)  # resolved: no board call
    return ctx


class ThreatModelTests(unittest.TestCase):
    def _refused(self, body, ctx) -> str:
        with self.assertRaises(SystemExit) as caught:
            post.validate_te_lead_fields(body, ctx)
        return str(caught.exception)

    def test_missing_field_refused_naming_both_forms(self):  # AC1
        message = self._refused(_handoff(None), _ctx())
        self.assertIn("good-faith", message)
        self.assertIn("hostile-author: <R-0371 kind>", message)

    def test_unfilled_field_refused(self):  # AC1
        message = self._refused(_handoff("{good-faith | hostile-author: <R-0371 kind>}"), _ctx())
        self.assertIn("no filled `Threat model:` line", message)  # the unfilled rule, not the vocabulary
        self.assertIn("hostile-author: <R-0371 kind>", message)

    def test_good_faith_accepted(self):
        post.validate_te_lead_fields(_handoff("good-faith"), _ctx())

    def test_value_outside_vocabulary_refused(self):  # AC2
        for value in ("hostile-author: whatever", "hostile", "Good-Faith", "hostile-author:"):
            with self.subTest(value=value):
                self._refused(_handoff(value), _ctx(tier="deep"))

    def test_every_r0371_kind_accepted_on_deep(self):  # AC2
        for kind in post.R0371_KINDS:
            with self.subTest(kind=kind):
                post.validate_te_lead_fields(_handoff(f"hostile-author: {kind}"), _ctx(tier="deep"))

    def test_hostile_author_needs_tier_deep(self):  # AC3
        message = self._refused(_handoff("hostile-author: auto-ae"), _ctx(tier="fast"))
        self.assertIn("R-0371", message)
        self.assertIn("deep", message)

    def test_unlabelled_issue_unaffected(self):  # AC4
        post.validate_te_lead_fields(_handoff(None), _ctx(labels={"bug"}))
        post.validate_te_lead_fields(_handoff("hostile-author: whatever"), _ctx(labels={"bug"}))

    def test_good_faith_never_reads_the_tier(self):  # AC6: tier() is lazy
        with mock.patch.object(post, "resolve_board_tier") as tier, \
                mock.patch.object(post, "resolve_project_board") as board:
            ctx = post.LeadCtx("vitalharmony/harmonic-forge", 1192, TE)
            post.validate_te_lead_fields(_handoff("good-faith"), ctx)
        tier.assert_not_called()
        board.assert_not_called()

    def test_tier_resolved_through_the_board_once(self):
        ctx = post.LeadCtx("vitalharmony/harmonic-forge", 1192, TE)
        with mock.patch.object(post, "resolve_project_board", return_value=("vitalharmony", "3")), \
                mock.patch.object(post, "resolve_board_tier", return_value="deep") as tier:
            post.validate_te_lead_fields(_handoff("hostile-author: auto-ae"), ctx)
            self.assertEqual(ctx.tier(), "deep")
        tier.assert_called_once_with("vitalharmony/harmonic-forge", "vitalharmony", "3", 1192)

    def test_rows_are_table_driven_with_ctx(self):  # AC6
        def needs_issue(value, ctx):
            return None if ctx.issue == 7 else f"wrong issue {ctx.issue}"
        rows = {**post.TE_LEAD_FIELDS, "Recognizer": needs_issue}
        body = _handoff("good-faith").replace("\n\n###", "\n**Recognizer:** none\n\n###")
        with mock.patch.object(post, "TE_LEAD_FIELDS", rows):
            post.validate_te_lead_fields(body, _ctx(issue=7))
            self.assertIn("wrong issue 1192", self._refused(body, _ctx(issue=1192)))
            self.assertIn("Recognizer", self._refused(_handoff("good-faith"), _ctx(issue=7)))

    def test_labels_read_once_per_post(self):  # AC7
        once = post._once(lambda: post.issue_labels("vitalharmony/harmonic-forge", 1192))
        body = _handoff("good-faith").replace("Lane 1 implements.", "Tooling Exception, Lane 1 implements.")
        with mock.patch.object(post, "issue_labels", return_value=set(TE)) as labels, \
                mock.patch.object(post.pitch_receipt, "refusal", return_value=None):
            post.validate_tooling_exception_labelled(body, "vitalharmony/harmonic-forge", 1192, once())
            post.validate_pitch_inspected("vitalharmony/harmonic-forge", 1192, once())
            post.validate_te_lead_fields(body, post.LeadCtx("vitalharmony/harmonic-forge", 1192, once()))
        self.assertEqual(labels.call_count, 1)

    def test_failed_label_read_is_not_retried(self):  # AC7, the failure path
        once = post._once(lambda: post.issue_labels("vitalharmony/harmonic-forge", 1192))
        with mock.patch.object(post, "issue_labels", side_effect=SystemExit("cannot read labels")) as labels:
            for _ in range(3):
                with self.assertRaises(SystemExit):
                    once()
        self.assertEqual(labels.call_count, 1)

    def test_main_shares_one_label_read(self):  # AC7, the wiring itself
        source = (ROOT / "l1_post.py").read_text()
        self.assertIn("labels_once = _once(lambda: issue_labels(repo, args.issue))", source)
        for call in ("validate_tooling_exception_labelled(body, repo, args.issue, labels_once())",
                     "validate_pitch_inspected(repo, args.issue, labels_once())",
                     "validate_te_lead_fields(body, LeadCtx(repo, args.issue, labels_once()))"):
            self.assertIn(call, source)


class TemplateTests(unittest.TestCase):  # AC5
    def test_template_carries_line_and_visible_table(self):
        text = (ROOT.parent.parent / "templates" / "lane1-handoff.md").read_text()
        visible = re.sub(r"<!--.*?-->", "", text, flags=re.S)
        self.assertIn("**Threat model:**", visible)
        self.assertIn("| Third parties", visible)
        self.assertIn("hostile-author", visible)

    def test_pitch_inspection_checks_the_declaration(self):
        text = (ROOT.parent.parent / "agents" / "pitch-inspection.md").read_text()
        self.assertIn("Threat model line against the design", text)


if __name__ == "__main__":
    unittest.main()
