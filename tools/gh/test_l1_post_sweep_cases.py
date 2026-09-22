#!/usr/bin/env python3
"""Tests for l1_post's sweep case-identifier handling (a private-repo incident)."""
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import l1_post as L  # noqa: E402

SPEC_LIST = (
    "## HITL Test Spec Review\n\n"
    "### Mandatory sequencing\n1. dry run\n2. apply\n\n"
    "### Test cases\n\n1. **A** x\n2. **B** y\n3. **C** z\n\n"
    "### Notes\n1. unrelated numbering\n"
)
SWEEP_LIST = (
    "## Gate-readiness sweep — H874\n\nWrite tier: R\n\n"
    "### Per-case readiness\n\n"
    "1. **A** executable as-is\n2. **B** executable\n3. **C** executable\n"
)
SPEC_TC = "### Test cases\n- TC1: a\n- TC2: b\n"
SWEEP_TC = "## Gate-readiness sweep — H874\n\nWrite tier: R\n\n### Test cases\n- TC1: ready, checked live\n- TC2: ready, checked live\n"
# a private-repo incident: the pre-execution sweep vocabulary. `pass`/`fail` are fabrications
# at sweep time; `ready`/`blocked` are both knowable before the gate runs.
SWEEP_TC_FABRICATED = "## Gate-readiness sweep — H874\n\nWrite tier: R\n\n### Test cases\n- TC1: pass all green\n- TC2: ready\n"
SPEC_LIST_WITH_PROSE_TC_MENTION = (
    "## HITL Test Spec Review\n\n"
    "**Basis for this spec:** ... and TC6 corrected in this redraft ...\n\n"
    "### Test cases\n\n1. **A** x\n2. **B** y\n3. **C** z\n"
    "4. **D** w\n5. **E** v\n6. **F** u\n"
)
SPEC_LIST_WITH_IN_SECTION_TC_CROSS_REFERENCE = (
    "## HITL Test Spec Review\n\n### Test cases\n\n"
    "1. **A** x\n2. **B** y\n3. **C** z\n"
    "4. **D** the write path\n"
    "5. **E** cleanup for TC4, with a final check.\n"
)
SPEC_TC_WITH_GROUP_SUBHEADINGS = (
    "## Test cases\n\n"
    "### Group A — verification gate\n\n"
    "**TC1 — first check.** Details here.\n\n"
    "### Group B — write path\n\n"
    "**TC2 — second check.** More details.\n\n"
    "## Executability summary\n\nNot a case.\n"
)
# a private-repo incident: harmonic-forge#401's real per-case heading style -- "test case"
# appears IN the per-case heading itself, which CASE_HEADING's vocabulary
# also matches. No genuine "### Test cases" section header exists at all.
SPEC_TC_HEADING_SAYS_TEST_CASE = (
    "## Lane 3 test spec — F401\n\n"
    "### TC1 — test case 1: the pattern is stated, all four conditions greppable\n"
    "Details for case 1.\n\n"
    "### TC2 — test case 2: the unproducible-only boundary is explicit, not a loophole\n"
    "Details for case 2.\n\n"
    "### TC3 — test case 3: the AE/role distinction is stated next to the AE definition\n"
    "Details for case 3.\n\n"
    "### TC4 — test case 4: block C is named as descoped, not silently dropped\n"
    "Details for case 4.\n\n"
    "### TC5 — test case 5: the incident naming a private-repo incident is preserved verbatim\n"
    "Details for case 5.\n\n"
    "### TC6 — test case 6: docs-check passes with no drift\n"
    "Details for case 6.\n\n"
    "### TC7 — test case 7: no application code is touched\n"
    "Details for case 7.\n\n"
    "### TC8 — test case 8: the four conditions are individually greppable\n"
    "Details for case 8.\n"
)
# a private-repo incident's real per-case heading style -- no "test case"/"per-case"/"case
# readiness" wording anywhere, so CASE_HEADING never matched at all and this
# spec always parsed correctly. Kept as a regression fixture: the fix must
# not change behavior for a spec that was never affected by the bug.
SPEC_TC_HEADING_NAMES_AC = (
    "## Lane 3 test spec — H1323\n\n"
    "### TC1 — AC1: ontology declarations and docs-check\n"
    "Details for case 1.\n\n"
    "### TC2 — AC3: advances_task_ids moves due_date, both owed_by values\n"
    "Details for case 2.\n\n"
    "### TC3 — AC2: raise, never a silent no-op\n"
    "Details for case 3.\n\n"
    "### TC4 — AC5: DERIVED_FROM has no application writer\n"
    "Details for case 4.\n\n"
    "### TC5 — edge direction cannot be confused with DERIVED_FROM\n"
    "Details for case 5.\n\n"
    "### TC6 — pre-execute dry-run: numbers must match before proceeding\n"
    "Details for case 6.\n\n"
    "### TC7 — the migration itself (Tier P, HITL-approved)\n"
    "Details for case 7.\n\n"
    "### TC8 — post-execute standing check reports zero\n"
    "Details for case 8.\n\n"
    "### TC9 — idempotency: a second dry-run finds nothing left to migrate\n"
    "Details for case 9.\n\n"
    "### TC10 — full verification gate, reproduced independently\n"
    "Details for case 10.\n\n"
    "### TC11 — AC6 has no test case\n"
    "Details for case 11.\n\n"
    "### TC12 — AC2's sole-writer claim is untested for ADVANCES\n"
    "Details for case 12.\n"
)


class CaseIdTests(unittest.TestCase):
    def test_case_heading_ignores_a_prose_phrase_without_markdown_heading(self):
        body = "Basis: use the Test cases section below.\n\n### Test cases\n1. first\n"
        self.assertEqual(L.case_ids(body), {"1"})

    def test_case_heading_limits_list_items_to_its_section(self):
        body = "### Test cases\n1. first\n\n### Evidence\n2. unrelated receipt\n"
        self.assertEqual(L.case_ids(body), {"1"})

    def test_list_item_ignores_a_numbered_basis_list(self):
        body = "### Basis\n1. copied example\n\n### Test cases\n2. actual case\n"
        self.assertEqual(L.case_ids(body), {"2"})

    def test_next_heading_stops_the_case_section(self):
        body = "### Test cases\n1. actual case\n\n### Notes\n2. not a case\n"
        self.assertEqual(L.case_ids(body), {"1"})

    def test_tc_identifiers_take_precedence(self):
        self.assertEqual(L.case_ids(SPEC_TC), {"1", "2"})

    def test_list_numbering_scoped_to_the_case_section(self):
        """Unrelated numbered lists elsewhere must not be counted."""
        self.assertEqual(L.case_ids(SPEC_LIST), {"1", "2", "3"})

    def test_sweep_per_case_heading_is_recognised(self):
        self.assertEqual(L.case_ids(SWEEP_LIST), {"1", "2", "3"})

    def test_edge_cases_heading_is_not_a_case_section(self):
        """A bare 'cases?' pattern would count the wrong list."""
        self.assertEqual(L.case_ids("### Edge cases\n1. empty\n2. boundary\n"), set())

    def test_no_case_section_yields_nothing(self):
        self.assertEqual(L.case_ids("## Spec\nprose only, no cases\n"), set())

    def test_prose_tc_mention_outside_case_section_does_not_preempt_list_numbering(self):
        """a private-repo incident: a "TC6" mention in a Basis paragraph must not pre-empt the
        spec's real, plainly-numbered 1..6 test-cases list."""
        self.assertEqual(
            L.case_ids(SPEC_LIST_WITH_PROSE_TC_MENTION), {"1", "2", "3", "4", "5", "6"})

    def test_tc_identifiers_inside_case_section_still_win_over_list_numbering(self):
        """Regression guard: a private-repo incident's original TC<n> fallback must still work
        when TC<n> markers are the case section's own numbering scheme."""
        body = "### Test cases\n- TC1: a\n- TC2: b\n- TC3: c\n"
        self.assertEqual(L.case_ids(body), {"1", "2", "3"})

    def test_group_subheadings_under_case_heading_do_not_empty_the_section(self):
        """a private-repo incident: a spec organizing its TC<n> cases under "### Group A"/
        "### Group B" sub-headings directly beneath "## Test cases" used to
        have its section cut to empty -- the first group sub-heading matched
        NEXT_HEADING immediately. A same-or-shallower heading ("##
        Executability summary") must still end the section."""
        self.assertEqual(L.case_ids(SPEC_TC_WITH_GROUP_SUBHEADINGS), {"1", "2"})

    def test_per_case_heading_containing_test_case_does_not_truncate_the_section(self):
        """a private-repo incident, live-reproduced against harmonic-forge#401's real spec:
        a per-case heading phrased "### TC1 -- test case 1: ..." also
        matches CASE_HEADING's "test case" vocabulary. Taking it as the
        section heading truncated the section to TC1's own body and lost
        TC2-TC8 entirely."""
        self.assertEqual(
            L.case_ids(SPEC_TC_HEADING_SAYS_TEST_CASE),
            {"1", "2", "3", "4", "5", "6", "7", "8"},
        )

    def test_ac_named_per_case_headings_still_parse_correctly(self):
        """a private-repo incident's real spec, AC3's regression fixture: no "test case"
        wording anywhere in its per-case headings, so this was never
        affected by the bug -- the fix must not change its result. TC11's
        own heading ("AC6 has no test case") does contain the phrase, which
        exercises the fix's TC_ID-exclusion on a heading that both carries
        an identifier AND the trigger vocabulary."""
        self.assertEqual(
            L.case_ids(SPEC_TC_HEADING_NAMES_AC),
            {str(n) for n in range(1, 13)},
        )

    def test_in_section_tc_cross_reference_does_not_shrink_list_numbering(self):
        """a private-repo incident follow-up 2: a later numbered case (5) can cross-reference
        an earlier one by number ("cleanup for TC4") from *inside* the actual
        test-cases section -- observed live on a private-repo incident's spec (case 12
        referencing "TC4"). The section's own 1..5 list numbering must win
        over that in-section mention, the same way it already wins over an
        out-of-section one."""
        self.assertEqual(
            L.case_ids(SPEC_LIST_WITH_IN_SECTION_TC_CROSS_REFERENCE),
            {"1", "2", "3", "4", "5"})


class ValidateSweepTests(unittest.TestCase):
    def _ok(self, sweep, spec, issue=874):
        try:
            L.validate_sweep(sweep, spec, "vitalharmony/hrse", issue)
            return True
        except SystemExit:
            return False

    def test_list_numbered_sweep_matching_spec_passes(self):
        """a private-repo incident: this combination previously could not be posted at all."""
        self.assertTrue(self._ok(SWEEP_LIST, SPEC_LIST))

    def test_missing_case_is_rejected(self):
        self.assertFalse(self._ok(SWEEP_LIST.replace("3. **C** executable\n", ""), SPEC_LIST))

    def test_extra_case_is_rejected(self):
        self.assertFalse(self._ok(SWEEP_LIST + "4. **D** executable\n", SPEC_LIST))

    def test_legacy_tc_path_unchanged(self):
        self.assertTrue(self._ok(SWEEP_TC, SPEC_TC))

    def test_tc_spec_needs_entries_not_outcomes(self):
        """a private-repo incident: supersedes test_tc_spec_still_requires_status_lines.

        A `TC<n>` sweep still needs one line-anchored entry per case, each
        naming its own case and carrying text -- but it must NOT state an
        outcome, because a gate-readiness sweep is posted before the gate
        runs. Two real entries with no outcome is the correct shape.
        """
        self.assertTrue(self._ok(
            "## Gate-readiness sweep — H874\n\nWrite tier: R\n\n### Test cases\n- TC1: a\n- TC2: b\n", SPEC_TC))

    def test_a_fabricated_outcome_is_rejected(self):
        """a private-repo incident AC4: no case has an outcome at sweep time."""
        self.assertFalse(self._ok(SWEEP_TC_FABRICATED, SPEC_TC))

    def test_blocked_stays_legal(self):
        """`blocked` is knowable pre-execution -- only pass/fail are not."""
        self.assertTrue(self._ok(
            "## Gate-readiness sweep — H874\n\nWrite tier: R\n\n"
            "### Test cases\n- TC1: blocked, no dev server\n- TC2: ready\n", SPEC_TC))

    def test_prose_naming_every_id_is_not_a_checklist(self):
        """a private-repo incident, found by the pre-close panel (4 of 5 refuters).

        `TC_ID` is unanchored, so an ID-set check alone is satisfied by N
        mentions in one sentence. testing-gate.md requires one line per TC,
        not a prose paragraph.
        """
        self.assertFalse(self._ok(
            "## Gate-readiness sweep — H874\n\nTC1 and TC2 are both ready.\n", SPEC_TC))

    def test_entries_must_name_their_own_case(self):
        """Entry count alone is not coverage -- the same case thrice is not two."""
        self.assertFalse(self._ok(
            "## Gate-readiness sweep — H874\n\n"
            "### Test cases\n- TC9: ready\n- TC9: ready\n", SPEC_TC))

    def test_entries_must_carry_text(self):
        """Bare ordinals satisfied the ID-set check and asserted nothing."""
        self.assertFalse(self._ok(
            "## Gate-readiness sweep — H874\n\n### Test cases\n- TC1:\n- TC2:\n", SPEC_TC))

    def test_a_spec_cannot_be_its_own_sweep(self):
        """A `*` bullet marker with no trailing space made `**TC1 ...**` parse
        as a list item, so a verbatim paste of the spec validated."""
        spec = "### Test cases\n\n**TC1 - a**\n\n**TC2 - b**\n"
        self.assertFalse(self._ok(spec, spec))

    def test_scanning_stays_scoped_to_the_case_section(self):
        """a private-repo incident's property, restated for a private-repo incident's contract.

        A copied `pass` example in a Basis block is neither counted as an
        entry nor flagged as a fabricated outcome -- both scans are scoped to
        the case section. The real entries below it are what certify.
        """
        sweep = (
            "## Gate-readiness sweep — H874\n\nWrite tier: R\n\n"
            "### Basis\n- TC1: pass copied receipt\n- TC2: blocked copied receipt\n\n"
            "### Per-case readiness\n- TC1: ready\n- TC2: ready\n"
        )
        self.assertTrue(self._ok(sweep, SPEC_TC))

    def test_spec_with_no_cases_is_rejected_with_guidance(self):
        self.assertFalse(self._ok(SWEEP_LIST, "## Spec\nprose only\n"))

    def test_spec_with_no_heading_at_all_gets_the_generic_error(self):
        with self.assertRaises(SystemExit) as ctx:
            L.validate_sweep(SWEEP_LIST, "## Spec\nprose only\n", "vitalharmony/hrse", 874)
        self.assertIn("no identifiable test cases", str(ctx.exception))

    def test_spec_with_a_heading_but_no_cases_names_the_isolated_section(self):
        """a private-repo incident AC4: when the parser isolates a real section and finds
        nothing in it, the error must name what it isolated rather than
        asserting the spec has no cases at all -- the misleading part of
        the original bug's symptom."""
        spec = "### Test cases\n\nJust prose, no TC markers and no numbered list.\n"
        with self.assertRaises(SystemExit) as ctx:
            L.validate_sweep(SWEEP_LIST, spec, "vitalharmony/hrse", 874)
        message = str(ctx.exception)
        self.assertIn("isolated test-case section", message)
        self.assertIn("Just prose", message)

    def test_headerless_sweep_is_rejected(self):
        """a private-repo incident: `## Gate-readiness sweep -- H<N>` is mechanically
        required by testing-gate.md (a private-repo incident) but was never checked here --
        a headerless sweep posted successfully and was only caught downstream
        by manual thread inspection."""
        with self.assertRaises(SystemExit) as ctx:
            L.validate_sweep(SWEEP_TC.replace("## Gate-readiness sweep — H874\n\n", ""), SPEC_TC, "vitalharmony/hrse", 874)
        self.assertIn("Gate-readiness sweep", str(ctx.exception))

    def test_list_numbered_sweep_accepted_when_spec_has_stray_tc_mention(self):
        """a private-repo incident follow-up: SPEC_LIST_WITH_PROSE_TC_MENTION uses plain 1..6
        list numbering for its actual case section but mentions "TC6" once in
        its Basis paragraph. A prose-style sweep (no "TCn: status" lines) must
        still be accepted -- the strict status-line format is for specs that
        use TC<n> as their real case-section numbering scheme, not specs that
        merely mention a case number in passing."""
        sweep = (
            "## Gate-readiness sweep — H886\n\nWrite tier: R\n\n### Per-case readiness\n\n"
            "1. executable as-is\n2. executable as-is\n3. executable as-is\n"
            "4. executable as-is\n5. executable as-is\n6. executable as-is\n"
        )
        self.assertTrue(self._ok(sweep, SPEC_LIST_WITH_PROSE_TC_MENTION, issue=886))


class OtherPatternAuditTests(unittest.TestCase):
    def test_template_placeholder_in_basis_does_not_invalidate_real_heading_content(self):
        body = "### Basis\n{url}\n\n### Issue\nReal issue context\n"
        self.assertTrue(L.is_substantive(L.heading_content(body, "Issue")))

    def test_heading_content_ignores_a_heading_name_mentioned_in_prose(self):
        body = "Basis says ### Issue is required.\n\n### Issue\nReal issue context\n"
        self.assertEqual(L.heading_content(body, "Issue"), "Real issue context")

    def test_project_board_comes_from_manifest_without_a_local_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "projects.toml"
            manifest.write_text(
                '[[project]]\nname="example"\nprefix="X"\n'
                'repo="example/project"\nonboarded=true\n'
                'board_owner="owner"\nboard_number="7"\n'
                '[project.protocol]\nworktree_name="{checkout}-lane{lane}"\n'
                'l1_post_task="l1-post"\nlane_comment_task="lane-comment"\n'
                'gate_checkout_task="gate-checkout"\nlane3_begin_task="lane3-begin"\n'
                'runs_lane3=true\n',
                encoding="utf-8",
            )
            with mock.patch.dict("os.environ", {"FORGE_PROJECTS_MANIFEST": str(manifest)}):
                self.assertEqual(
                    L.resolve_project_board("example/project", Path("/missing")),
                    ("owner", "7"),
                )

    def test_comment_target_ignores_issuecomment_text_outside_url_fragment(self):
        with self.assertRaises(SystemExit):
            L.validate_comment_target(
                "https://github.com/vitalharmony/hrse/issues/900?note=issuecomment-123",
                "vitalharmony/hrse", 900,
            )

    def test_source_repo_match_requires_a_remote_path_boundary(self):
        with mock.patch.object(L, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="https://github.com/example/vitalharmony/hrse\n")
            self.assertFalse(L._source_repo_is_hrse(Path(".")))
            run.return_value = mock.Mock(returncode=0, stdout="https://github.com/vitalharmony/hrse.git\n")
            self.assertTrue(L._source_repo_is_hrse(Path(".")))


if __name__ == "__main__":
    unittest.main()
