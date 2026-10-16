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
# harmonic-forge#796: a partial AE (H2115-style) authorizes only the changed
# case; the sweep still lists every spec case, with the un-authorized ones
# marked "carried forward, not re-run" rather than omitted.
SPEC_TC5 = "### Test cases\n- TC1: a\n- TC2: b\n- TC3: c\n- TC4: d\n- TC5: e\n"
SWEEP_TC5_CARRY_FORWARD = (
    "## Gate-readiness sweep — H2115\n\nWrite tier: W\n\n### Per-case readiness\n\n"
    "1. TC1 — carried forward, not re-run: evidence in issuecomment-1; code at abc123 unchanged since.\n"
    "2. TC2 — ready to run: accept-with-correction live.\n"
    "3. TC3 — carried forward, not re-run: evidence in issuecomment-1; code at abc123 unchanged since.\n"
    "4. TC4 — carried forward, not re-run: evidence in issuecomment-1; code at abc123 unchanged since.\n"
    "5. TC5 — carried forward, not re-run: evidence in issuecomment-1; code at abc123 unchanged since.\n"
)
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

    def test_partial_ae_carry_forward_sweep_is_accepted(self):
        """harmonic-forge#796: a sweep naming all 5 spec cases, with 4 marked
        'carried forward, not re-run' and 1 'ready to run', is a legal sweep
        for a partial AE -- the full ID-set is present, so validate_sweep's
        exact-match check passes, and no case states an outcome."""
        self.assertTrue(self._ok(SWEEP_TC5_CARRY_FORWARD, SPEC_TC5, issue=2115))

    def test_carry_forward_sweep_still_rejects_a_fabricated_outcome(self):
        """A carry-forward line naming its evidence is legal; a carry-forward
        line that instead asserts pass/fail is not -- SWEEP_FABRICATED_OUTCOME
        applies identically to carry-forward entries."""
        sweep = SWEEP_TC5_CARRY_FORWARD.replace(
            "3. TC3 — carried forward, not re-run: evidence in issuecomment-1; code at abc123 unchanged since.\n",
            "3. TC3 — pass: evidence in issuecomment-1.\n",
        )
        self.assertFalse(self._ok(sweep, SPEC_TC5, issue=2115))

    def test_narrowing_to_one_case_is_rejected_by_the_exact_match_check(self):
        """harmonic-forge#796 AC4/TC3: reverting the carry-forward fixture to a
        1-case sweep must fail with the exact-match message -- proof this
        fixture actually exercises that path, not a parser accepting anything."""
        with self.assertRaises(SystemExit) as ctx:
            L.validate_sweep(
                "## Gate-readiness sweep — H2115\n\nWrite tier: W\n\n"
                "### Per-case readiness\n\n1. TC2 — ready to run: accept-with-correction live.\n",
                SPEC_TC5, "vitalharmony/hrse", 2115,
            )
        self.assertIn("sweep case IDs must exactly match spec", str(ctx.exception))

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
                'repo="example/project"\naccount="vitalharmony"\nonboarded=true\nworkspace="vh"\n'
                'board_owner="owner"\nboard_number="7"\n'
                '[project.protocol]\nworktree_name="{checkout}-lane{lane}"\n'
                'l1_post_task="l1-post"\nlane_comment_task="lane-comment"\n'
                'gate_checkout_task="gate-checkout"\nlane3_begin_task="lane3-begin"\n'
                'lane3_end_task="lane3-end"\ngate_task="check"\n'
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



class FooterCaseIdsTests(unittest.TestCase):
    """harmonic-forge#1000: a spec's `classes=` footer names its cases; prose never does."""

    REPO, ISSUE = "vitalharmony/openclaw-projects", 40

    @staticmethod
    def _spec(rows, footer_ids, prose=""):
        table = "\n".join(f"| TC{c} | ac | x | y | z |" for c in rows)
        footer = ("<!-- l1-post v1; kind=spec; posted-by=LANE3; classes="
                  + ",".join(f"{c}:ac" for c in footer_ids) + " -->") if footer_ids is not None else ""
        return f"## Lane 3 Test Spec — O40\n\n{prose}\n\n### Cases\n{table}\n\n{footer}"

    @staticmethod
    def _sweep(ids, numbered=False):
        lines = "\n".join(
            (f"{i}. step {i} ready" if numbered else f"- TC{c}: ready — offline fixtures")
            for i, c in enumerate(ids, 1))
        return f"## Gate-readiness sweep — O40\n\nWrite tier: R\n\n### Per-case readiness\n{lines}\n"

    def _validate(self, sweep, spec):
        L.validate_sweep(sweep, spec, self.REPO, self.ISSUE)

    def test_a_prose_tc_mention_is_not_a_case_when_the_footer_names_them(self):
        """O40's spec: TC5's text cites "the O35 TC10 digest"; nine cases."""
        ids = [str(i) for i in range(1, 10)]
        spec = self._spec(ids, ids, prose="TC5 replays the O35 TC10 digest.")
        self._validate(self._sweep(ids), spec)

    def test_a_suffixed_case_id_from_the_footer_validates(self):
        """O38's spec issuecomment-6170246608: footer `...,9:existing,7b:ac`."""
        ids = [str(i) for i in range(1, 10)] + ["7b"]
        spec = self._spec(ids, ids)
        self.assertIn("7b", L.footer_case_ids(spec))
        self._validate(self._sweep(ids), spec)

    def test_a_missing_case_is_still_refused(self):
        ids = [str(i) for i in range(1, 10)] + ["7b"]
        with self.assertRaises(SystemExit):
            self._validate(self._sweep([i for i in ids if i != "7b"]), self._spec(ids, ids))

    def test_an_extra_prose_case_in_the_sweep_is_refused(self):
        ids = [str(i) for i in range(1, 10)]
        with self.assertRaises(SystemExit):
            self._validate(self._sweep(ids + ["10"]), self._spec(ids, ids, prose="the O35 TC10 digest"))

    def test_a_numbered_list_sweep_against_a_footer_spec_is_refused(self):
        ids = ["1", "2", "3"]
        with self.assertRaises(SystemExit):
            self._validate(self._sweep(ids, numbered=True), self._spec(ids, ids))

    def test_an_absent_or_missing_footer_takes_todays_path(self):
        """Without a usable footer the prose `TC10` still counts, exactly as before."""
        ids = [str(i) for i in range(1, 10)]
        for footer in (None, ["absent"]):
            with self.subTest(footer=footer):
                spec = self._spec(ids, None, prose="the O35 TC10 digest")
                if footer:
                    spec += "\n<!-- l1-post v1; kind=spec; classes=absent -->"
                with self.assertRaises(SystemExit):
                    self._validate(self._sweep(ids), spec)
                self._validate(self._sweep(ids + ["10"]), spec)

    def test_a_repeated_case_line_is_refused(self):
        ids = ["1", "2", "3"]
        with self.assertRaises(SystemExit) as caught:
            self._validate(self._sweep(["1", "1", "2", "3"]), self._spec(ids, ids))
        self.assertIn("repeated", str(caught.exception))

    def test_the_shared_outcome_pattern_is_unchanged_without_a_footer(self):
        self.assertIsNone(L.SWEEP_FABRICATED_OUTCOME.search("- TC2a passed"))
        self.assertIsNotNone(L.SWEEP_FABRICATED_OUTCOME_SUFFIXED.search("- TC2a passed"))

    def test_a_bold_outcome_word_is_refused_and_readiness_prose_is_not(self):
        ids = ["1", "2"]
        ok = self._sweep(ids).replace("- TC1: ready — offline fixtures",
                                      "- TC1: ready — the gate will fail closed if the fetch fails")
        self._validate(ok, self._spec(ids, ids))
        for line in ("- TC1: **passed**", "- TC1: Passed."):
            with self.subTest(line=line), self.assertRaises(SystemExit):
                sweep = self._sweep(ids).replace("- TC1: ready — offline fixtures", line)
                self._validate(sweep, self._spec(ids, ids))

    def test_an_unbulleted_extra_or_repeated_case_line_is_refused(self):
        ids = ["1", "2"]
        for extra in ("TC3: ready", "TC1: ready again"):
            with self.subTest(extra=extra), self.assertRaises(SystemExit):
                self._validate(self._sweep(ids) + extra + "\n", self._spec(ids, ids))

    def test_mixed_ids_sort_without_crashing(self):
        self.assertEqual(sorted({"10", "7b", "7", "2"}, key=L._case_sort_key), ["2", "7", "7b", "10"])



# harmonic-forge#1022: the shape of hrse#1896's Tier P spec (6206077379):
# cases P1..P3 in a table under a bare "### Cases" heading, then a later
# section with its own numbered list and a stray "P2".
SPEC_TIER_P = (
    "## Lane 3 Test Spec — H1896 (Tier P production run, R-0377)\n\n"
    "**Cases:** 3, Tier P through the runner only.\n\nWrite tier: P\n\n"
    "### Cases\n| # | Class | Action | Command | Pass when |\n|---|---|---|---|---|\n"
    "| P1 | ac | Dry run | `run` | totals reported |\n"
    "| P2 | ac | Apply | `run --mode apply` | moved equals P1's count |\n"
    "| P3 | ac | Dry run again | the P1 command | 0 re-anchor |\n\n"
    "### Preconditions for Lane 1's sweep\n1. CI green.\n2. backup verified before P2.\n"
    "3. grant minted.\n4. runner installed.\n"
)
SWEEP_TIER_P = (
    "## Gate-readiness sweep — H1896\n\n**Readiness:** P1 now.\n**Blockers:** none.\n"
    "**Next:** Lane 3 runs P1.\n\nWrite tier: P\n\n### Per-case readiness\n"
    "1. P1 — ready: grant minted.\n2. P2 — not authorized by this AE.\n"
    "3. P3 — not authorized by this AE.\n"
)


class TierPCaseIdTests(unittest.TestCase):
    def test_tier_p_spec_case_ids(self):
        self.assertEqual(L.case_ids(SPEC_TIER_P), {"1", "2", "3"})

    def test_stray_p_outside_the_case_section_adds_nothing(self):
        spec = SPEC_TIER_P + "\n### Notes\nSee P7 and Tier P9.\n"
        self.assertEqual(L.case_ids(spec), {"1", "2", "3"})

    def test_list_sweep_validates_against_tier_p_spec(self):
        with mock.patch.object(L, "footer_case_ids", return_value=set()):
            L.validate_sweep(SWEEP_TIER_P, SPEC_TIER_P, "vitalharmony/hrse", 1896)

    def test_edge_cases_heading_still_not_a_case_heading(self):
        self.assertFalse(L._case_section("### Edge cases\n1. x\n")[1])

    def test_tc_spec_unchanged(self):
        self.assertEqual(L._classify_case_ids(SPEC_TC), ({"1", "2"}, True))


if __name__ == "__main__":
    unittest.main()
