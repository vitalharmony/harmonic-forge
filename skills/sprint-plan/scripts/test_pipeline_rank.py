#!/usr/bin/env python3
"""Tests for pipeline_rank.py (hrse#1489).

Both live false positives produced while designing the rule are regression
tests here, and so is each recorded limit — a limit that is not pinned is a
limit that silently changes.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pipeline_rank as pr  # noqa: E402


class EnglishNounsAreNotCodeSurfaces(unittest.TestCase):
    """Constraint (a). An early map matched `\\bBLOCKS\\b` and `\\bPerson\\b`,
    and hrse#1489's own opening line "Blocks #1477" classified the issue as
    tier-3 opportunity work. Cross-repo dependency vocabulary collides with
    domain vocabulary."""

    def test_blocks_as_dependency_language_is_not_the_BLOCKS_edge(self):
        r = pr.rank("Extend the triage", "Blocks #1477. Extends #1476.",
                    {"tech-debt"})
        self.assertNotEqual(r.group, "3")
        self.assertNotIn("opportunity", r.stages)

    def test_bare_english_person_and_company_do_not_match(self):
        r = pr.rank("Improve onboarding",
                    "A Person joining a Company should feel welcome.",
                    {"feature"})
        self.assertNotIn("record", r.stages)

    def test_the_cypher_labels_do_match(self):
        r = pr.rank("x", "Dedupe `:Company` nodes by name.", {"tech-debt"})
        self.assertIn("record", r.stages)
        self.assertEqual(r.group, "3")


class OffPipelineIsAbsenceNotPresence(unittest.TestCase):
    """Constraint (b). An earlier version let OFF_PIPELINE win outright and
    sent hrse#911 to tier 4, because that issue names `mise run graph-hygiene`
    as its delivery mechanism while being about `:Company` identity."""

    def test_a_tooling_delivery_mechanism_does_not_beat_a_real_stage(self):
        r = pr.rank(
            "Duplicate Company detection",
            "Surfaced via `mise run graph-hygiene`; merges `:Company` nodes.",
            {"tech-debt"})
        self.assertEqual(r.group, "3")

    def test_off_pipeline_only_is_tier_4(self):
        r = pr.rank("Wrapper parity", "Assert `mise.toml` exposes tools/gh/ flags.",
                    {"tech-debt"})
        self.assertEqual(r.group, "4")
        self.assertIn("off-pipeline", r.why)

    def test_the_shared_memory_store_tooling_is_tier_4_not_unclassified(self):
        """harmonic-forge#500. `tools/memory/` had no pattern here, so an
        issue naming only it fell through to UNCLASSIFIED — "nobody has
        evaluated this" — rather than tier 4's "real work, no pipeline
        leverage". F500 and F494 both classified that way; the work is
        correctly tier 4 and only the pattern was missing."""
        r = pr.rank(
            "Memory frontmatter backfill and the aging check",
            "Backfill `first_seen:`/`instances:` in `tools/memory/`.",
            {"tooling"})
        self.assertEqual(r.group, "4")
        self.assertIn("off-pipeline", r.why)

    def test_naming_nothing_is_UNCLASSIFIED_not_tier_4(self):
        """Tier 4 asserts 'no pipeline leverage this cycle'. That is a claim
        nobody has evaluated for an issue naming no surface at all, so it must
        not be asserted by default -- the operator required this split."""
        r = pr.rank("The dashboard feels wrong", "It is confusing to read.",
                    {"ui"})
        self.assertEqual(r.group, pr.GROUP_UNCLASSIFIED)
        self.assertIn("no code surface", r.why)


class TierAssignment(unittest.TestCase):
    BODY = "Fix `opportunity_service.py`'s lane guard."

    def test_later_label_short_circuits(self):
        self.assertEqual(pr.rank("x", self.BODY, {"later", "bug"}).group,
                         pr.GROUP_LATER)

    def test_capability_labels_give_tier_1(self):
        self.assertEqual(pr.rank("x", self.BODY, {"feature"}).group, "1")

    def test_correctness_labels_give_tier_3(self):
        self.assertEqual(pr.rank("x", self.BODY, {"bug"}).group, "3")

    def test_external_clock_beats_capability_and_correctness(self):
        r = pr.rank("x", self.BODY + " Google's verification review deadline.",
                    {"feature", "bug"})
        self.assertEqual(r.group, "2")

    def test_the_opt_in_label_is_a_tier_2_escape_hatch(self):
        """forge#96 matched the CLOCK lexicon; forge#97, its sibling in the
        same OAuth thread, did not -- which is why the label exists."""
        r = pr.rank("x", self.BODY, {pr.CLOCK_LABEL, "feature"})
        self.assertEqual(r.group, "2")
        self.assertIn(pr.CLOCK_LABEL, r.why)

    def test_stage_matched_but_labels_decide_neither(self):
        r = pr.rank("x", self.BODY, {"infrastructure"})
        self.assertEqual(r.group, pr.GROUP_UNCLASSIFIED)
        self.assertIn("decide neither", r.why)

    def test_internal_accumulation_is_not_an_external_clock(self):
        """Tier 2 is 'doing nothing makes it worse ON ITS OWN' -- an outside
        clock, not internal drift."""
        r = pr.rank("x", self.BODY + " Duplicates accumulate over time.",
                    {"bug"})
        self.assertEqual(r.group, "3")


class HarmonicHistoryVocabulary(unittest.TestCase):
    """Recorded limit 4, which came due immediately: the Episode/Assertion/
    Commitment architecture shipped in hrse#1435/#1436, and a map without it
    silently dropped the entire 2.9 Episodes thread to UNCLASSIFIED."""

    def test_episode_and_source_record_are_capture(self):
        r = pr.rank("Episodes: three-way split",
                    "Split migrated blobs into `:Episode` + `:SourceRecord`.",
                    {"feature"})
        self.assertIn("capture", r.stages)
        self.assertEqual(r.group, "1")

    def test_assertion_lineage_is_record(self):
        r = pr.rank("x", "Maintain `assertion_ids` on the lineage index.",
                    {"tech-debt"})
        self.assertIn("record", r.stages)

    def test_commitment_is_followup(self):
        r = pr.rank("x", "`:Commitment` receipts must survive retraction.",
                    {"bug"})
        self.assertIn("followup", r.stages)


class EpicInheritance(unittest.TestCase):
    def test_epic_inherits_the_most_urgent_child(self):
        own = pr.Ranking(pr.GROUP_UNCLASSIFIED, "names no code surface at all")
        self.assertEqual(pr.inherit_epic_group(own, ["3", "1", "4"]).group, "1")

    def test_no_rankable_children_leaves_it_alone(self):
        own = pr.Ranking(pr.GROUP_UNCLASSIFIED, "names no code surface at all")
        got = pr.inherit_epic_group(own, [pr.GROUP_LATER, pr.GROUP_UNCLASSIFIED])
        self.assertEqual(got.group, pr.GROUP_UNCLASSIFIED)

    def test_an_epic_already_more_urgent_than_its_children_keeps_its_own(self):
        own = pr.Ranking("1", "capture + capability")
        self.assertEqual(pr.inherit_epic_group(own, ["3", "4"]).group, "1")

    def test_inheritance_states_where_the_tier_came_from(self):
        own = pr.Ranking(pr.GROUP_UNCLASSIFIED, "names no code surface at all")
        self.assertIn("inherited", pr.inherit_epic_group(own, ["2"]).why)


class ReadsOnlyTitleAndBody(unittest.TestCase):
    def test_comments_are_not_an_input(self):
        """Comments are volatile and would cost a second REST sweep per
        issue. The signature makes that structural, not a convention."""
        import inspect
        params = set(inspect.signature(pr.rank).parameters)
        self.assertEqual(params, {"title", "body", "labels"})

    def test_a_null_body_is_tolerated(self):
        self.assertEqual(pr.rank("x", None, set()).group, pr.GROUP_UNCLASSIFIED)


class ReadinessLabelTests(unittest.TestCase):
    """hrse#1523 AC2 — `not-ready` is authoritative, and only the label."""

    def test_the_label_demotes_to_NOT_READY(self):
        got = pr.rank("Some feature", "A capability body.",
                      {"feature", pr.NOT_READY_LABEL})
        self.assertEqual(got.group, pr.GROUP_NOT_READY)
        self.assertIn(pr.NOT_READY_LABEL, got.why)

    def test_absence_of_the_label_never_demotes_anything(self):
        """An unlabelled issue ranks exactly as it does today — AC2's other
        half. Same body/labels as a plain capability issue, no readiness
        signal, no label: ranks "1" unchanged."""
        got = pr.rank("Some feature", "profile_service change.", {"feature"})
        self.assertEqual(got.group, "1")


class ReadinessHintTests(unittest.TestCase):
    """hrse#1523 AC3 — prose hints, never demotes. #303/#304/#1025 verbatim."""

    SCOPE_CAPTURE_BODY = (
        "Generalize both scheduled patterns for Cy, reading from "
        "opportunity_service state.\n\n"
        "Pick up via a `product-strategy` pass when actually prioritized — "
        "this issue is scope-capture, not a spec."
    )
    DESIGN_GATED_BODY = (
        "Child of #258 (EPIC: Cy). Requested by the operator. "
        "**Filed pending a robust design — a `product-strategy` pass must "
        "run before any Lane 1 handoff.**"
    )

    def test_scope_capture_shape_is_hinted_hrse303_304(self):
        got = pr.rank("Cy: scheduled self-checking", self.SCOPE_CAPTURE_BODY,
                      {"feature"})
        self.assertEqual(got.hint, "scope-capture-not-a-spec")

    def test_design_gated_shape_is_hinted_hrse1025(self):
        got = pr.rank("Cy: offer to extract commitments",
                      self.DESIGN_GATED_BODY, {"feature"})
        self.assertEqual(got.hint, "design-gated-before-handoff")

    def test_a_hinted_but_unlabelled_issue_keeps_its_current_group(self):
        """AC3's falsification case: the prose alone must never move `group`."""
        got = pr.rank("Cy: scheduled self-checking", self.SCOPE_CAPTURE_BODY,
                      {"feature"})
        self.assertEqual(got.group, "1")

    def test_no_hint_when_no_shape_matches(self):
        got = pr.rank("Ordinary feature", "profile_service change.", {"feature"})
        self.assertIsNone(got.hint)

    def test_hint_and_label_can_coexist_label_still_wins_the_group(self):
        got = pr.rank("Cy: scheduled self-checking", self.SCOPE_CAPTURE_BODY,
                      {"feature", pr.NOT_READY_LABEL})
        self.assertEqual(got.group, pr.GROUP_NOT_READY)
        self.assertEqual(got.hint, "scope-capture-not-a-spec")


class RatchetGapsHrse1653(unittest.TestCase):
    """hrse#1653 — the three patterns added after the ratchet failed at 44.4%,
    and every case that forced one of them to be narrower.

    The rejected variants are pinned as hard as the accepted ones. A rejection
    that is not a test is a rejection the next session re-proposes.
    """

    # --- the local dev-stack OFF_PIPELINE gap (hrse#600) -------------------

    def test_local_dev_stack_lands_in_group_4_not_unclassified(self):
        """#600 verbatim. Group 4 says "no pipeline leverage this cycle";
        UNCLASSIFIED says "nobody has evaluated this". The second is a
        different and worse claim about work that is plainly dev tooling."""
        got = pr.rank(
            "BUG/tech-debt: local stack self-heal — process-compose "
            "liveness+restart, Neo4j/podman restart policies, dashboard.py "
            "--json", "", {"tech-debt"})
        self.assertEqual(got.group, "4")

    def test_each_dev_stack_token_reaches_group_4_on_its_own(self):
        for token in ("process-compose", "podman-compose", "containers-up",
                      "containers-down", "containers-status", "db-heal",
                      "pc-up"):
            with self.subTest(token=token):
                got = pr.rank(f"Fix {token}", "", {"tech-debt"})
                self.assertEqual(got.group, "4")

    def test_the_dashboard_router_is_not_an_off_pipeline_surface(self):
        """`dashboard.py` was proposed for OFF_PIPELINE and rejected.
        `backend/app/api/routers/dashboard.py` is a product router, so this
        issue would render as "no pipeline leverage this cycle" — false, and
        invisible to `check_ratchet`, which only sees UNCLASSIFIED."""
        got = pr.rank(
            "Dashboard counts are stale after a sync",
            "The aggregate query in `app/api/routers/dashboard.py` reads a "
            "cached value and never invalidates it.", {"bug"})
        self.assertEqual(got.group, pr.GROUP_UNCLASSIFIED)

    def test_podman_as_a_prerequisite_mention_does_not_demote_hrse1458(self):
        """hrse#1458 verbatim. Bare `\\bpodman\\b` sent a GDPR federation
        feature to Group 4 on a parenthetical naming its own dependencies."""
        got = pr.rank(
            "GDPR: geofenced federation mode (EU-only instance bridging)",
            "(BACKLOG-009 Phase 3 prerequisite chain — Podman/Keycloak/MinIO "
            "physical instance isolation.)", {"feature"})
        self.assertNotEqual(got.group, "4")

    # --- the PDF capture gap (hrse#397, hrse#586) -------------------------

    def test_pdf_parse_bug_reaches_the_capture_stage(self):
        """#586 verbatim, and its body really is empty — the title is all the
        ranker gets."""
        got = pr.rank(
            "PDF parse errors don't distinguish AI provider billing/quota "
            "rejection from network failure", "", {"bug"})
        self.assertEqual(got.group, "3")
        self.assertIn("capture", got.stages)

    def test_pdf_parse_bug_reaches_capture_via_a_later_occurrence_hrse397(self):
        """#397 verbatim, and it is the fragile one of the three fixed targets.

        The title's FIRST `pdf` — in `issue178-pdf-name-preservation` — sits
        about 64 characters from the nearest `pars` and does NOT satisfy the
        60-character lookahead. The match survives only on the second
        occurrence, `after PDF parse`. #586's fixture spans 1 character, so it
        cannot exercise this at all: narrowing the span, or any change to how
        `rank()` concatenates title and body, would regress #397 silently
        while every other test in this class still passed.
        """
        title = ("BACKLOG-N: issue178-pdf-name-preservation TC-2 fails live "
                 "— name field empty after PDF parse")
        got = pr.rank(title, "", {"bug"})
        self.assertEqual(got.group, "3")
        self.assertIn("capture", got.stages)

    def test_hrse397_first_pdf_occurrence_alone_is_out_of_lookahead_range(self):
        """Pins the claim above rather than asserting it in a docstring: with
        the trailing `after PDF parse` removed, the title no longer matches."""
        got = pr.rank(
            "BACKLOG-N: issue178-pdf-name-preservation TC-2 fails live "
            "— name field empty", "", {"bug"})
        self.assertNotIn("capture", got.stages)

    def test_pdf_in_a_format_list_is_not_capture_work_forge15(self):
        """harmonic-forge#15 verbatim. It genuinely parses PDFs — for a
        different product's ingestion — and a bare `\\bpdfs?\\b` promoted it to
        Group 1, which claims it changes the operator's daily job/opp work."""
        got = pr.rank(
            "POR ingestion: integrate Unstructured for format-agnostic "
            "document extraction",
            "Wire in format-parsing for POR documents — PDFs, native Google "
            "Docs, Microsoft formats (.docx).", {"feature"})
        self.assertNotIn("capture", got.stages)

    def test_pdf_named_in_a_feature_inventory_is_not_capture_work_hrse174(self):
        """hrse#174 verbatim. One line of a mobile epic's surface inventory.
        An `ingest` alternative in the lookahead re-admitted it, and giving it
        a stage also switched the clock lexicon to the looser `CLOCK`, which
        promoted it to Group 2 on "Refresh token rotation" — the false
        positive `CLOCK_EXTERNAL` names by number in its own docstring."""
        got = pr.rank(
            "EPIC: CymaGraph Mobile Companion",
            "Covers dictation, topology canvas, company merge, settings, "
            "PDF ingestion.\n\nRefresh token rotation ON.", {"epic"})
        self.assertNotIn("capture", got.stages)
        self.assertNotEqual(got.group, "2")

    def test_a_passing_mention_of_pdf_tests_is_not_capture_work_hrse1280(self):
        """hrse#1280 verbatim — an aside inside an `await` bug."""
        got = pr.rank(
            "Live e2e cannot run against any vertical",
            "The cause is a missing `await` on `read_upload` — the mocked "
            "PDF/docx tests stayed green with the `await` removed.",
            {"tech-debt"})
        self.assertNotIn("capture", got.stages)

    def test_parse_vocabulary_before_pdf_does_not_match(self):
        """Forward-only. The reverse form re-admitted harmonic-forge#15."""
        got = pr.rank("Parsing work", "format-parsing for documents — PDFs.",
                      {"feature"})
        self.assertNotIn("capture", got.stages)

    # --- the apiClient extension, measured and rejected -------------------

    def test_apiclient_is_not_a_runtime_surface(self):
        """Rejected, not overlooked. `CLAUDE.md` forbids raw `fetch()`
        elsewhere, so every frontend issue names `apiClient` whatever it is
        about — which makes it a mandate, not a signal. It falsely promoted
        hrse#142, #214 and #241 to Group 1."""
        got = pr.rank("SMS 3: Pulse events + compose/send UI",
                      "Sends through `apiClient`.", {"feature", "ui"})
        self.assertNotIn("runtime", got.stages)

    def test_hrse161_stays_unclassified_and_that_is_the_honest_answer(self):
        """The one target this issue did NOT fix. A frontend
        dependency-adoption issue naming no pipeline stage; a rank bought with
        four misclassifications is worse than no rank."""
        got = pr.rank(
            "Adopt Zod v4 at the apiClient boundary",
            "Validate with `safeParse` in `queryFn` rather than "
            "`schema.parse(json)`. Adds `zod` to `frontend/package.json`.",
            {"feature"})
        self.assertEqual(got.group, pr.GROUP_UNCLASSIFIED)

    # --- the baseline itself ----------------------------------------------

    def test_the_ratchet_baseline_was_not_moved(self):
        """The constant's whole purpose is to be hard to move. Lowering it
        against a 9-issue milestone would encode noise as a standard, and
        raising it is the failure it exists to expose."""
        self.assertEqual(pr.UNCLASSIFIED_RATCHET_BASELINE, (7, 44))


if __name__ == "__main__":
    unittest.main()
