#!/usr/bin/env python3
"""Tests for l1_post's per-artifact lead block (harmonic-forge#472, a private-repo incident).

Outcome first, evidence collapsed. The lead is artifact-SPECIFIC, not one
universal verdict/finding/next schema: a gate-readiness sweep is a
pre-execution artifact with no verdict, and an AE is an authorization that
reports no finding. Forcing them into one template produces headings that lie.

a private-repo incident extended coverage from the original two kinds (sweep, ae) to five
more (handoff, ready-for-l3, rework, spec, gate-result) and added a byte cap
on the lead region itself. The per-artifact discipline above is unchanged —
only the count of covered artifacts grew.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import l1_post as L  # noqa: E402

SWEEP = (
    "## Gate-readiness sweep — H1\n\n"
    "**Readiness:** all 2 cases executable as written.\n"
    "**Blockers:** none.\n"
    "**Next:** Lane 3 executes, then reports PASS/FAIL.\n\n"
    "Write tier: R\n\n"
    "### Per-case readiness\n\n1. TC1 — ready.\n2. TC2 — ready.\n"
)
AE = (
    "## AE — H1\n\n"
    "**Authorized:** the spec in issuecomment-123, against `feat/x` @ `abc1234`.\n"
    "**Next:** Lane 3 executes the approved cases and reports.\n\n"
    "Operator approved in session.\n"
)
SPEC = (
    "## Lane 3 Test Spec — H1\n\n"
    "**Cases:** 2 (write tier R).\n**Next:** submit for HITL approval.\n\n"
    "### Test cases\n1. TC1 — a thing.\n2. TC2 — another.\n"
)
GATE_RESULT = (
    "## Lane 3 Gate Results — H1\n\n"
    "**Verdict:** PASS\n**Finding:** none.\n**Next:** merge.\n\n"
    "### TC1 — PASS\nEvidence here.\n"
)
HANDOFF = (
    "## Handoff: H1 — x\n\n"
    "**Scope:** the guard only.\n**Next:** Implement #1.\n\n"
    "### Issue\ncontent\n"
)
READY_FOR_L3 = (
    "# ready-for-l3 — H1\n\n"
    "**Target:** `feat/x` @ `abc1234`.\n**Verified:** mypy clean, diff scope matches.\n"
    "**Next:** Lane 3 gates the standard spec.\n\n"
    "### Lane 1 review\ndetail\n"
)
REWORK = (
    "## L1 — rework, H1\n\n"
    "**Finding:** branch is behind origin/main.\n**Next:** rebase and re-push.\n\n"
    "### Detail\nmore\n"
)


class LeadPresence(unittest.TestCase):
    def test_a_conforming_sweep_and_ae_pass(self) -> None:
        L.validate_lead("sweep", SWEEP)
        L.validate_lead("ae", AE)

    def test_each_missing_sweep_field_is_named(self) -> None:
        for label in ("Readiness", "Blockers", "Next"):
            with self.subTest(label=label):
                stripped = "\n".join(line for line in SWEEP.splitlines()
                                     if not line.startswith(f"**{label}:**"))
                with self.assertRaises(SystemExit) as ctx:
                    L.validate_lead("sweep", stripped)
                self.assertIn(f"`**{label}:** ...`", str(ctx.exception))

    def test_an_ae_needs_what_it_authorized_and_what_is_next(self) -> None:
        for label in ("Authorized", "Next"):
            with self.subTest(label=label):
                stripped = "\n".join(line for line in AE.splitlines()
                                     if not line.startswith(f"**{label}:**"))
                with self.assertRaises(SystemExit):
                    L.validate_lead("ae", stripped)

    def test_an_ae_is_not_asked_for_a_finding_it_does_not_have(self) -> None:
        """The semantic half of the ratified correction: an AE grants
        permission. A `Readiness` or `Blockers` field on it would be a heading
        that lies, so it is not required and its absence is not a refusal."""
        self.assertEqual(L.LEAD_FIELDS["ae"], ("Authorized", "Next"))
        self.assertNotIn("Readiness", L.LEAD_FIELDS["ae"])
        L.validate_lead("ae", AE)

    def test_a_sweep_is_not_asked_for_a_verdict(self) -> None:
        """`validate_sweep` already refuses a sweep stating pass/fail — it is
        a PRE-execution artifact. The lead must not reintroduce one."""
        self.assertNotIn("Verdict", L.LEAD_FIELDS["sweep"])
        self.assertNotIn("Finding", L.LEAD_FIELDS["sweep"])

    def test_ae_and_sweep_is_not_a_real_kind_to_this_function(self) -> None:
        """`ae-and-sweep` is decomposed into separate `validate_lead("ae", ...)`
        / `validate_lead("sweep", ...)` calls in `main()` — it is never itself
        passed here, so it correctly has no LEAD_FIELDS entry of its own."""
        L.validate_lead("ae-and-sweep", "no lead here at all")

    def test_the_five_kinds_hrse1703_added_all_pass_conforming_bodies(self) -> None:
        for kind, body in (
            ("handoff", HANDOFF), ("ready-for-l3", READY_FOR_L3),
            ("rework", REWORK), ("spec", SPEC), ("gate-result", GATE_RESULT),
        ):
            with self.subTest(kind=kind):
                L.validate_lead(kind, body)

    def test_each_new_kind_names_its_missing_field(self) -> None:
        cases = {
            "handoff": (HANDOFF, "Scope"),
            "ready-for-l3": (READY_FOR_L3, "Target"),
            "rework": (REWORK, "Finding"),
            "spec": (SPEC, "Cases"),
            "gate-result": (GATE_RESULT, "Verdict"),
        }
        for kind, (body, label) in cases.items():
            with self.subTest(kind=kind):
                stripped = "\n".join(line for line in body.splitlines()
                                     if not line.startswith(f"**{label}:**"))
                with self.assertRaises(SystemExit) as ctx:
                    L.validate_lead(kind, stripped)
                self.assertIn(f"`**{label}:** ...`", str(ctx.exception))

    def test_a_gate_result_is_not_forced_to_restate_pass_fail_in_prose(self) -> None:
        """A BLOCKED gate is a legitimate third outcome
        (`post_lane_discussion.py`'s own `KIND_HEADING` note) — the lead
        requires a `Verdict` line, not a specific value."""
        blocked = (
            "## Lane 3 Gate Results — H1\n\n"
            "**Verdict:** BLOCKED\n**Finding:** no fixture available.\n"
            "**Next:** provision the fixture, then retest.\n"
        )
        L.validate_lead("gate-result", blocked)


class LeadPosition(unittest.TestCase):
    """AC1: *before* any evidence. Presence alone is not the requirement —
    the whole defect is a finding that is present, in the last paragraph."""

    def test_a_lead_below_the_evidence_is_refused(self) -> None:
        buried = (
            "## Gate-readiness sweep — H1\n\n"
            "### Per-case readiness\n\n1. TC1 — ready.\n2. TC2 — ready.\n\n"
            "**Readiness:** all ready.\n**Blockers:** none.\n**Next:** go.\n"
        )
        with self.assertRaises(SystemExit):
            L.validate_lead("sweep", buried)

    def test_a_lead_inside_a_details_block_is_refused(self) -> None:
        """The shape this issue introduces is exactly the one that could hide
        the lead. `<details>` terminates the region."""
        collapsed = (
            "## Gate-readiness sweep — H1\n\n"
            "<details><summary>Evidence</summary>\n\n"
            "**Readiness:** all ready.\n**Blockers:** none.\n**Next:** go.\n\n"
            "</details>\n"
        )
        with self.assertRaises(SystemExit):
            L.validate_lead("sweep", collapsed)

    def test_the_region_stops_at_whichever_comes_first(self) -> None:
        self.assertNotIn("hidden", L.lead_region(
            "## X\n\nlead\n\n<details>\nhidden\n</details>\n"))
        self.assertNotIn("hidden", L.lead_region("## X\n\nlead\n\n### E\nhidden\n"))

    def test_the_artifact_heading_does_not_terminate_the_region(self) -> None:
        """`##` is the artifact's own heading, the line the lead follows.
        Terminating on it would make every conforming lead invisible."""
        self.assertIn("**Next:**", L.lead_region(SWEEP))


class SweepValidationStillPasses(unittest.TestCase):
    """AC5, reproduced live before it was fixed rather than reasoned about."""

    def test_a_sweep_with_a_lead_still_validates_against_its_spec(self) -> None:
        L.validate_sweep(SWEEP, SPEC, "vitalharmony/hrse", 1)

    def test_a_lead_naming_a_case_does_not_inject_a_phantom_id(self) -> None:
        """The concrete collision. With no `### Per-case readiness` heading,
        `_case_section` falls back to the whole body — so `**Next:** re-gate
        TC3` made a correct sweep fail with
        `expected ['1','2'], got ['1','2','3']`."""
        sweep = (
            "## Gate-readiness sweep — H1\n\n"
            "**Readiness:** both ready.\n**Blockers:** none.\n"
            "**Next:** re-gate TC3 after the fix.\n\n"
            "Write tier: R\n\n1. TC1 — ready.\n2. TC2 — ready.\n"
        )
        L.validate_sweep(sweep, SPEC, "vitalharmony/hrse", 1)

    def test_stripping_lead_lines_does_not_hide_a_real_case(self) -> None:
        """The strip is line-scoped to those labels. A case entry is
        `1. TC1 — ...`, which no `**Label:** ...` line can be — so a spec that
        uses bare TC<n> markers and no heading at all still resolves, which is
        the shape a private-repo incident exists to keep working.

        `Coverage` (not `Cases`) stands in as the arbitrary non-label word
        here — a private-repo incident made `Cases` itself a real LEAD_LINE label, so it no
        longer demonstrates "a lookalike word is not stripped"; it would now
        demonstrate the opposite."""
        self.assertEqual(L.case_ids("Coverage: TC1 and TC2 both matter.\n"), {"1", "2"})
        self.assertEqual(
            L.case_ids("**Next:** ignore\n\n### Test cases\n- TC4 — x.\n"), {"4"})

    def test_a_specs_own_cases_lead_line_does_not_inject_a_phantom_id(self) -> None:
        """The concrete new collision a private-repo incident introduces: a spec now
        legitimately carries `**Cases:** 2 (write tier R).` in its own lead —
        this must be stripped before case-scanning the same way `**Next:**
        re-gate TC3` already is, or a conforming spec would fail
        `validate_sweep`'s own case-id cross-check against itself."""
        self.assertEqual(L.case_ids(SPEC), {"1", "2"})


if __name__ == "__main__":
    unittest.main()
