#!/usr/bin/env python3
"""hrse#1489 — rank an issue into one of the four groups in
`docs/PRIORITIES.md` § "How work is ranked — the groups" (hrse#1522:
renamed from "tiers" — that word means the board's fast/standard/deep
model-routing field everywhere else in this codebase).

**The input is the code surface an issue names**, matched against a stage map
transcribed from the doc's own pipeline sentence, combined with the issue's
existing labels. Operator decision 2026-09-02; full rationale and the rejected
alternatives are recorded on hrse#1489.

Three signals, all already maintained by existing tooling for other reasons:

| signal          | source                          | who maintains it |
|-----------------|---------------------------------|------------------|
| pipeline surface| code identifiers in title+body  | nobody — a byproduct of the handoff format already requiring a named entry point (86% hrse / 79% forge coverage, measured) |
| change kind     | GitHub labels                   | `gh_issue.py`, which already requires `--labels` (191/192 hrse, 80/80 forge) |
| external clock  | `CLOCK` lexicon + `pipeline-clock` label | nobody, plus opt-in |

**The load-bearing property: `STAGE`'s keys are a transcription of a sentence
the doc already commits to**, not an invented taxonomy. `PRIORITIES.md` defines
the pipeline as "capture (dictation, meeting transcripts, email) -> a readable
relationship record -> deciding who to contact and what to say -> tracking the
opportunity -> not dropping the follow-up." If the thesis changes, that sentence
changes and this map follows it — one home per fact, derived from the definition
rather than parallel to it.

**Why not the obvious candidates** (each rejected on evidence, hrse#1489):

- `Theme` is degenerate on the population that matters — 9 of 10 open 2.8
  issues are `Substrate`. It also answers "what capability," which the doc
  explicitly says is not a sequence.
- `Sequence` is **already dead**: 25/191 (13%) populated on open items,
  decaying by exactly the mechanism that killed `Priority`.
- A new *required* board field repeats hrse#839/#966 verbatim — `Priority` had
  508 items carrying a value, 277 claiming NOW/NEXT against ~40 the doc
  described. Exhaustive fields decay into false signal, which is worse than
  none.
- LLM body classification is ruled out by `forge_pipeline_triage.py`'s own
  docstring.

**Known limits, recorded rather than papered over.** Tier 1's real definition
("something he hit while actually using the product") is not derivable from
text; what is implemented is the weaker claim "adds capability to a pipeline
stage," and any rendering must say so. Tier 2 is a claim about the world, not
the text — the `CLOCK` lexicon caught forge#96 and missed forge#97, its sibling
in the same OAuth thread, which is why `pipeline-clock` exists. And evidence
held outside the issue is invisible: hrse#911 ranks tier 3 here (Company
identity is textually tier 3) where a human says tier 4, because the tier-4
evidence is a measurement recorded in a different document. The operator
overrides at read time.
"""
from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass

#: One key per stage of the pipeline sentence in `docs/PRIORITIES.md`.
#:
#: **Match only code-shaped tokens** — path fragments, `snake_case`
#: identifiers, Cypher labels, edge types, property accessors. Never a bare
#: capitalized English word: an early version matched `\bBLOCKS\b` and
#: `\bPerson\b`, and hrse#1489's own opening line "Blocks #1477" classified
#: the issue as opportunity work. Cross-repo dependency vocabulary collides
#: with domain vocabulary.
STAGE: dict[str, str] = {
    "capture": (
        r"dictation_engine|pdf_ingestion|pdf_extraction|ingestion_pipeline"
        r"|transcript_attach|transcript_service|transcript_providers"
        r"|meeting_persistence|meeting_extraction|calendar_sync_service"
        r"|gmail_tools|background_sync_service|google_auth_service"
        r"|cy_drive_delivery|drive_service|\bSCOPES\b|routers/auth\.py"
        r"|gmail\.(?:compose|readonly)|calendar\.readonly"
        # Harmonic History (hrse#1431 epic): Episode/SourceRecord are the
        # capture surface. Shipped in hrse#1435/#1436 -- absent from an
        # earlier map, which silently dropped the entire 2.9 Episodes
        # thread (11 issues) to UNCLASSIFIED.
        r"|:Episode|:SourceRecord|Episode\.\w+|SourceRecord\.\w+"
        r"|episode_lifecycle_service|episode_service|FROM_SOURCE"
        r"|\bepisodes?\b(?=[^.]*\b(?:ingest|source|segment|quiescen|blob|transcript)\b)"
        # The write-path half of the same epic. The lookahead above gates
        # bare `episodes?` on backend vocabulary, which UI-shaped bodies
        # ("evolved pills", "the one delete button", "multi-person",
        # "composer") never satisfy — so hrse#1437/#1439/#1441/#1443 named
        # the ontology repeatedly and still ranked UNCLASSIFIED. These issues
        # write the labels bare and backticked (`SourceRecord` + `Episode` +
        # `INVOLVES`) rather than colon-prefixed, which the patterns above
        # require.
        #
        # `(?-i:...)` keeps these CASE-SENSITIVE inside an `re.I` search, and
        # that is load-bearing, not tidiness: case-insensitive `\bINVOLVES\b`
        # matches the ordinary English verb "involves", which is the
        # `\bBLOCKS\b`-matching-"Blocks #1477" failure this map was built to
        # avoid. Capitalized `Episode`/`SourceRecord`/`Commitment` are Cypher
        # labels in this repo's prose; lowercase "episode" is English.
        r"|(?-i:\bSourceRecord\b|\bEpisode\b|\bINVOLVES\b)"
        r"|occurred_at|SharedHistoryDictation"
        # hrse#1653: the map held only the MODULE names (`pdf_ingestion`,
        # `pdf_extraction`), and two live PDF-ingestion bugs — #397 and #586 —
        # say "PDF parse" in English and name no module, so both fell to
        # UNCLASSIFIED ("nobody has evaluated this") rather than to a capture
        # stage.
        #
        # This is the one place the map admits a token that is not a code
        # identifier, and the exception is narrow on purpose: `pdf` is a FORMAT
        # ACRONYM, not an English word, so it does not carry the English/domain
        # second meaning that made `\bBLOCKS\b` match "Blocks #1477".
        #
        # **A bare `\bpdfs?\b` was measured and is NOT what shipped.** Across
        # 221 open issues it moved four, two of them wrong: harmonic-forge#15
        # (`PDFs` in a format list, promoted to Group 1 — a different product's
        # document ingestion, not the operator's daily job/opp work) and
        # hrse#1280 (`the mocked PDF/docx tests`, an aside in an `await` bug).
        # A format name alone says an issue MENTIONS PDFs, not that it is about
        # ingesting one.
        #
        # The lookahead is what carries the claim. Same idiom as the `episodes?`
        # gate above, for the same reason — and it is narrower than it first
        # looks, because measurement forced it narrower twice:
        #
        #   * `ingest|extract` in the lookahead re-admitted harmonic-forge#15
        #     ("format-parsing for POR documents — PDFs", a format list for a
        #     DIFFERENT product's ingestion) and hrse#174 ("settings, PDF
        #     ingestion", one line of a mobile epic's feature inventory). Both
        #     are real mentions of real PDF work; neither issue is ABOUT
        #     parsing a PDF. Text alone cannot separate "is about" from "lists
        #     among many", so the pattern stops trying.
        #   * A reverse form (parse-vocabulary BEFORE `pdf`) re-admitted
        #     harmonic-forge#15 on its own. Forward-only.
        #
        # **Stated limit:** an issue reading "PDF ingestion is broken" will
        # still rank UNCLASSIFIED. That is deliberate — the alternative
        # measured worse — and it is the kind of gap to fix by naming the
        # module in the issue, not by loosening this.
        r"|\bpdfs?\b(?=[^.]{0,60}\bpars\w*)"
    ),
    "record": (
        r"profile_service|profile_enrichment|contacts\.py|experience_service"
        r"|entity_resolution|channel_mirror|activity_service|vectorizer\.py"
        r"|embeddings\.py|enrichment_service|:Person|:Company|:Email|:Phone"
        r"|:Experience|:Activity|CURRENTLY_WORKS_AT|FORMERLY_WORKED_AT"
        r"|HAS_EXPERIENCE|Person\.\w+|Company\.\w+|create_experience"
        r"|create_activity|resolve_anchor"
        r"|:Assertion|Assertion\.\w+|assertion_ids|episode_ids"
        r"|ASSERTED_IN|SUPPORTED_BY|assertion_lineage"
        # Same epic, staging-UI surface: the components hrse#1439 replaces
        # and the field it renders. See the capture stage's note on `(?-i:)`.
        r"|source_quote|DictationStagingPanel|DictationRelationshipPill"
        r"|(?-i:\bRelationshipType\b|\bAssertion\b)"
    ),
    "decide": (
        r"match_service|intro_path_service|relationship_health"
        r"|relationship_embeddings|stewardship_service|sentiment_scorer"
        r"|discovery_\w+|llm_gateway|model_discovery_service|pursuit_ranked"
        r"|job.?scout"
    ),
    "opportunity": (
        r"opportunity_service|opportunity_queries|opportunity_task_queries"
        r"|:Opportunity|Opportunity\.\w+|VALID_LANES|merge_key|nascent"
    ),
    "followup": (
        r"commit_task|update_task|:Task|Task\.\w+|health_scheduler"
        r"|create_task|planned_week|task_lifecycle_service"
        r"|:Commitment|Commitment\.\w+|opportunity_writeback"
        r"|(?-i:\bCommitment\b)"
    ),
    "runtime": (
        r"app/database\.py|init_driver|close_driver|GraphDatabase\.driver"
        r"|app/main\.py|\blifespan\b|identity_gateway|JWKS|SessionMiddleware"
        r"|require_auth"
        # hrse#1653 considered adding `apiClient`/`src/api/client.ts` here, to
        # classify #161 (Zod at that boundary). MEASURED AND REJECTED — recorded
        # so it is not re-proposed.
        #
        # The reasoning was that `apiClient` is a chokepoint: `CLAUDE.md`
        # forbids raw `fetch()` anywhere else, so every frontend call passes
        # through it. That mandate is exactly what makes it useless as a signal
        # — it means every frontend issue NAMES it, whatever the issue is about.
        # Across all 221 open issues the pattern moved five, only one of them
        # the target: #142 (a UI panel), #214 (an Expo scaffold) and #241 (an
        # SMS compose UI) were all falsely promoted to Group 1, and #174 to
        # Group 2 by giving it a stage — which switches the clock lexicon from
        # `CLOCK_EXTERNAL` to the looser `CLOCK`, reactivating the "Refresh
        # token rotation" false positive that `CLOCK_EXTERNAL` names by number
        # in its own docstring.
        #
        # #161 therefore stays UNCLASSIFIED, correctly: it is a frontend
        # dependency-adoption issue that names no pipeline stage, and the
        # ranker saying so is more honest than a rank bought with four
        # misclassifications.
    ),
}

#: Surfaces that are work *about* the work.
#:
#: **Consulted only when no `STAGE` pattern matched.** An earlier version let
#: this win outright and sent hrse#911 to tier 4, because that issue names
#: `mise run graph-hygiene` as its *delivery mechanism* while being about
#: `Company` identity. Off-pipeline is properly the ABSENCE of a pipeline
#: token; this pattern exists only to separate "names an off-pipeline surface"
#: (tier 4) from "names no code surface at all" (UNCLASSIFIED), which the
#: operator required as a strict reading of "never silently default into
#: tier 4."
OFF_PIPELINE = (
    r"sprint-plan|milestone_summary|drift_check|repo_hygiene|tools/gh/"
    r"|gh_issue\.py|item_list_cache|\blane[123]\b|mise\.toml"
    r"|\.github/workflows|docs/[A-Z]|README\.md|CLAUDE\.md|\.claude/rules/"
    r"|forge_pipeline_triage|publish_sprint_summary|l1_post|l2_post"
    r"|3-lane-protocol|\.claude/|tools/hooks/|pipeline_rank"
    # harmonic-forge#500: the shared memory store's tooling. Without this,
    # an issue naming only `tools/memory/` matched no STAGE and no
    # OFF_PIPELINE pattern, so it fell through to UNCLASSIFIED — "nobody has
    # evaluated this", which is a different and worse claim than "real work,
    # no pipeline leverage this cycle". F500 itself classified that way, and
    # so did F494 before it; the work is correctly Group 4, and the ranker
    # simply had no pattern for the surface it names.
    r"|tools/memory/"
    # hrse#1653: the local dev-stack surface. #600 names `process-compose`
    # liveness/restart, podman restart policies and `dashboard.py --json` —
    # real work with no pipeline leverage, which is precisely Group 4. With no
    # pattern here it fell PAST Group 4 into UNCLASSIFIED, which asserts the
    # different and worse claim that nobody has evaluated it. Identical shape
    # to the `tools/memory/` case directly above.
    #
    # **`dashboard\.py` was proposed here and deliberately left out.**
    # `backend/app/api/routers/dashboard.py` is a first-class product router,
    # named in `CLAUDE.md`'s router list, and no `STAGE` key covers it — so an
    # issue about stale dashboard counts is stage-less, reaches this pattern,
    # and renders under "no pipeline leverage this cycle" with a `why` line
    # that is simply false. Worse than the UNCLASSIFIED it replaced, because
    # UNCLASSIFIED is the only state `check_ratchet` can see: the miss would be
    # invisible forever. It also buys nothing — #600 matches `process-compose`
    # without it, and every other `dashboard.py` in the tree is already covered
    # by `sprint-plan` or `\.claude/`. This is the hrse#911 demotion
    # (`mise run graph-hygiene` as a delivery mechanism) with the
    # stage-wins-first guard inert, because the dashboard router names no stage.
    #
    # **`podman` is matched only as `podman-compose`**, for the same reason at
    # smaller scale. Bare `\bpodman\b` demoted hrse#1458 (GDPR geofenced
    # federation) to Group 4 on "(BACKLOG-009 Phase 3 prerequisite chain —
    # Podman/Keycloak/MinIO physical instance isolation)" — a *dependency* of
    # the product work it describes, not the surface it touches. The four
    # genuine container issues (harmonic-forge#26/#66, hrse#986/#1135) all
    # write `podman-compose`, so the narrower token keeps every true positive
    # and drops the false one. Federation vocabulary is absent from `STAGE`,
    # which is why the stage-wins-first guard does not protect #1458.
    r"|process-compose|podman-compose|containers-(?:up|down|status)"
    r"|db-heal|pc-up"
)

#: An EXTERNAL clock only — something outside the repo makes this worse on its
#: own if untouched. Internal accumulation is tier 3 drift, not tier 2.
#:
#: Applied only where a STAGE pattern already matched. On that path the
#: issue has demonstrated it is about the pipeline, so the looser terms are
#: safe.
CLOCK = (
    r"expir(?:e|es|ing|y|ation)|verification (?:review|deadline)"
    r"|risks rejection|certificat\w* renew|token rotat"
    r"|deprecat(?:ed|ion)|end.of.life|sunset(?:ting)?\b|shuts? down on"
    r"|removed in v?\d|api version.{0,20}retir|quota exhaust|\bEOL\b"
)

#: The STAGE-LESS path's clock lexicon: externally anchored terms only.
#:
#: Moving the clock check ahead of the stage-less bail is what makes the
#: `pipeline-clock` label reachable at all — but running the full `CLOCK`
#: there promotes five issues on domain vocabulary rather than an external
#: clock, measured population-wide:
#:
#:     #192 Trust Loop   "Lapsed — ... quietly expired"   (a ledger state)
#:     #39  HITL         "72 hours (inherits bridge TTL)" (an internal TTL)
#:     #31  Trust Bridge "SET key value EX ttl ... EXPIRE"(a Redis command)
#:     #24  Pulse Hint   "time-to-live expiry"            (an internal TTL)
#:     #174 Mobile epic  "Refresh token rotation ON"      (a design choice)
#:
#: That is the same failure class as `\bBLOCKS\b` matching "Blocks #1477" —
#: an English or domain word colliding with the lexicon — reappearing inside
#: `CLOCK` once it was promoted to the first substantive step. So the
#: stage-less path drops generic `expir*` and `token rotat`, which are the
#: two terms every one of those five matched on, and keeps only terms that
#: name something outside this repo. `#197` ("Sunset RapidAPI LinkedIn post
#: sync") still reaches tier 2 on `sunset` alone, with no label needed.
CLOCK_EXTERNAL = (
    r"deprecat(?:ed|ion)|end.of.life|sunset(?:ting)?\b|shuts? down on"
    r"|removed in v?\d|api version.{0,20}retir|quota exhaust|\bEOL\b"
    r"|risks rejection|verification (?:review|deadline)|certificat\w* renew"
)

CAPABILITY_LABELS = frozenset({"feature", "ui", "career-vertical"})
CORRECTNESS_LABELS = frozenset({"bug", "tech-debt"})
DEFERRAL_LABEL = "later"
#: Sparse, opt-in escape hatch for tier 2 (hrse#1489). Deliberately NOT a
#: board field: `Priority` died because it was required and exhaustive, so it
#: decayed into confident falsehood. A sparse opt-in label fails the other
#: way -- unapplied, the tier-2 group is visibly empty rather than wrong.
CLOCK_LABEL = "pipeline-clock"
EPIC_LABEL = "epic"
#: hrse#1523: same sparse-opt-in design as `DEFERRAL_LABEL`/`CLOCK_LABEL`,
#: for the same reason -- an unapplied readiness label must leave a group
#: visibly wrong (still ranked as if ready) rather than confidently wrong
#: (silently demoted on a phrase match). The label is authoritative; prose
#: is a hint only, never a demotion -- see `READINESS_HINTS` below.
NOT_READY_LABEL = "not-ready"

GROUP_LATER = "LATER"
GROUP_UNCLASSIFIED = "UNCLASSIFIED"
#: hrse#1523: an issue this ranker would otherwise place in group 1-4, but
#: which is NOT currently pickable -- carries the `not-ready` label, or (in
#: `forge_pipeline_triage.classify()`, which has the cross-issue state this
#: module does not) is blocked on a still-open dependency or is an epic with
#: no code of its own. Terminal, like LATER/UNCLASSIFIED: it does not
#: participate in `_GROUP_RANK`'s urgency ordering.
GROUP_NOT_READY = "NOT_READY"

#: hrse#1523: two of the three not-ready shapes measured live (2026-09-02,
#: hrse#199/#258/#303/#304/#1025) are prose, not a label -- `#303`/`#304`
#: read "this issue is scope-capture, not a spec" and `#1025` reads "a
#: `product-strategy` pass must run before any Lane 1 handoff", verbatim.
#: The third shape (an epic with no code of its own) already has a label
#: (`EPIC_LABEL`) and needs no prose match.
#:
#: **Advisory only, deliberately** -- mirrors `forge_pipeline_triage.py`'s
#: own stated reasoning for keeping its A/B/C bucket a label-and-citation
#: fact rather than an LLM read: a phrase matcher promoted to authoritative
#: would demote real work on a coincidental phrase match. It is surfaced as
#: a hint next to the row instead, so the `not-ready` label actually gets
#: applied and the label does the demoting.
READINESS_HINTS: dict[str, re.Pattern[str]] = {
    "scope-capture-not-a-spec": re.compile(
        r"scope.capture,?\s*not\s+a\s+spec", re.I),
    "design-gated-before-handoff": re.compile(
        r"product.strategy.{0,60}pass\s+must\s+run\s+before\s+any\s+lane"
        r".?\s*1\s+handoff", re.I),
}


def _readiness_hint(text: str) -> str | None:
    for name, pattern in READINESS_HINTS.items():
        if pattern.search(text):
            return name
    return None


@dataclass(frozen=True)
class Ranking:
    group: str  # "1" | "2" | "3" | "4" | "LATER" | "UNCLASSIFIED" | "NOT_READY"
    why: str
    stages: tuple[str, ...] = ()
    #: hrse#1523: an advisory not-ready SHAPE the body's prose matched
    #: (`READINESS_HINTS` key), independent of `group` -- never changes the
    #: rank on its own. `None` when no prose shape matched, regardless of
    #: whether the issue is otherwise demoted.
    hint: str | None = None


def _stages(text: str) -> tuple[str, ...]:
    return tuple(s for s, pattern in STAGE.items()
                 if re.search(pattern, text, re.I))


def rank(title: str, body: str | None, labels: set[str]) -> Ranking:
    """Rank one issue. Ordered; first match wins.

    Reads title and body only -- never comments, which are volatile and would
    cost a second REST sweep per issue.

    hrse#1523: every branch below runs unchanged; the readiness hint
    (`READINESS_HINTS`, advisory-only) is computed once and attached to
    whichever `Ranking` this returns via `dataclasses.replace`, rather than
    threading it through each `return` -- so a hint can never influence
    which branch fires.
    """
    text = f"{title}\n{body or ''}"
    ranking = _rank_group(text, labels)
    return dataclasses.replace(ranking, hint=_readiness_hint(text))


def _rank_group(text: str, labels: set[str]) -> Ranking:
    if DEFERRAL_LABEL in labels:
        return Ranking(GROUP_LATER, f"{DEFERRAL_LABEL!r} label")
    if NOT_READY_LABEL in labels:
        return Ranking(GROUP_NOT_READY, f"{NOT_READY_LABEL!r} label")

    stages = _stages(text)
    named = "/".join(stages) if stages else "no stage"

    # The clock check runs BEFORE the stage-less bail (amended ordering,
    # hrse#1489). Under the original ordering a stage-less issue returned
    # UNCLASSIFIED first, so `pipeline-clock` was unreachable and so was the
    # keyword path — for exactly the population tier 2 exists to hold, since
    # tier 2's natural members are stage-less third-party-service issues.
    # #197 is the live case: its body contains "sunset" and it names no code
    # surface at all, so under the old order it could never reach here even
    # with the label applied by hand.
    if CLOCK_LABEL in labels:
        return Ranking("2", f"{named} + {CLOCK_LABEL!r} label", stages)
    lexicon = CLOCK if stages else CLOCK_EXTERNAL
    if re.search(lexicon, text, re.I):
        return Ranking("2", f"{named} + external clock", stages)

    if not stages:
        if re.search(OFF_PIPELINE, text, re.I):
            return Ranking("4", "names an off-pipeline surface only")
        # NOT tier 4: tier 4 asserts "no pipeline leverage this cycle", which
        # is a claim nobody has evaluated for an issue that names nothing.
        return Ranking(GROUP_UNCLASSIFIED, "names no code surface at all")
    if labels & CAPABILITY_LABELS:
        return Ranking(
            "1", f"{named} + capability {sorted(labels & CAPABILITY_LABELS)}",
            stages)
    if labels & CORRECTNESS_LABELS:
        return Ranking(
            "3", f"{named} + correctness {sorted(labels & CORRECTNESS_LABELS)}",
            stages)
    return Ranking(
        GROUP_UNCLASSIFIED,
        f"{named} but labels={sorted(labels) or 'none'} decide neither",
        stages)


#: Group ordering for "maximum urgency" — 1 is the most urgent, so it wins.
_GROUP_RANK = {"1": 0, "2": 1, "3": 2, "4": 3}


def inherit_epic_group(own: Ranking, child_groups: list[str]) -> Ranking:
    """An epic inherits the MAXIMUM (most urgent) group of its open children.

    An epic states a container, not a change, so the rule has nothing to read
    from its own body -- every hrse UNCLASSIFIED case measured was a bare
    `epic` label. Inheriting is honest where guessing would not be.
    """
    ranked = [t for t in child_groups if t in _GROUP_RANK]
    if not ranked:
        return own
    best = min(ranked, key=lambda t: _GROUP_RANK[t])
    if own.group in _GROUP_RANK and _GROUP_RANK[own.group] <= _GROUP_RANK[best]:
        return own
    return Ranking(best, f"epic; inherited from open children (max of {sorted(set(ranked))})",
                   own.stages)


#: The share of open issues in the current milestone allowed to rank
#: UNCLASSIFIED — a **ratchet**, not a threshold chosen to pass.
#:
#: Measured 2026-09-02 against milestone 2.9's 44 open issues: **7
#: UNCLASSIFIED, 15.9%**. The REFORGE that produced this constant recorded
#: 29% (13 of 45) before the stage map gained the Harmonic-History write-path
#: vocabulary; the four issues it named by number — #1437, #1439, #1441,
#: #1443 — now rank tier 1, and the improvement came from extending `STAGE`,
#: not from moving this number.
#:
#: The direction is fail-if-worse. Lowering it after a genuine improvement is
#: the intended use; raising it to make a run pass is the failure this
#: constant exists to make visible, and doing so silently defeats the point.
#: Stored as the measured COUNTS, not a rounded rate. `0.159` excluded the
#: very measurement it was derived from — 7/44 is 15.909%, which is greater
#: than 15.9% — so the ratchet failed against its own baseline. A rounded
#: constant here is a silent off-by-a-hair in the direction that blocks.
UNCLASSIFIED_RATCHET_BASELINE = (7, 44)
UNCLASSIFIED_RATCHET = UNCLASSIFIED_RATCHET_BASELINE[0] / UNCLASSIFIED_RATCHET_BASELINE[1]


def unclassified_rate(rankings: list[Ranking]) -> float:
    """Share of `rankings` that came out UNCLASSIFIED. 0.0 for an empty list —
    no issues is not a classification failure."""
    if not rankings:
        return 0.0
    return sum(1 for r in rankings if r.group == GROUP_UNCLASSIFIED) / len(rankings)


def check_ratchet(rankings: list[Ranking],
                  limit: float = UNCLASSIFIED_RATCHET) -> tuple[bool, str]:
    """`(ok, message)`. Pure, so the ratchet is testable without the network."""
    rate = unclassified_rate(rankings)
    count = sum(1 for r in rankings if r.group == GROUP_UNCLASSIFIED)
    if rate > limit:
        return False, (
            f"UNCLASSIFIED ratchet FAILED: {count}/{len(rankings)} = "
            f"{rate:.1%} exceeds the {limit:.1%} baseline. Extend STAGE to "
            f"cover what these issues name — do not raise the baseline."
        )
    return True, (f"UNCLASSIFIED {count}/{len(rankings)} = {rate:.1%} "
                  f"(baseline {limit:.1%})")
