#!/usr/bin/env python3
"""a private-repo incident AC4: the refusal demonstrated against real over-long artifacts,
not only synthetic ones — a private-repo incident's own Lane 3 Test Spec, `ready-for-l3`
and gate-result comments, the obvious corpus this issue's own body names.

All three bodies below are verbatim (fetched `gh api
repos/vitalharmony/hrse/issues/comments/<id> --jq .body`). Provenance is the
comment id in each constant's name, not a claim re-derived from memory.

**Revised after preclose-inspection (5-agent panel, 2026-09-08).** The first
version of this file measured `lead_region()` against a broken implementation
that treated every `##` line as never terminating the lead — not just the
artifact's own opening one — so it mis-stated `REAL_READY_FOR_L3_5579216165`
as having "no `###` heading" (it has three `##` ones) and mis-measured its
region at 2,825 bytes instead of the true 250. That bug, and the corpus
measurement that replaced the original ~950-byte cap estimate (wrong by 2x)
with the real observed maximum, are both `lead_region()`'s and
`LEAD_CAP_BYTES`'s own history now — see their docstrings/comments in
`l1_post.py`. This file's job is narrower: prove the *current* code refuses
what it should, against real content, not synthetic approximations of it.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import l1_post as L  # noqa: E402

# issuecomment-5579228305 on vitalharmony/a private-repo incident, fetched 2026-09-08.
# No `###`/`##` heading and no `<details>` block anywhere in the body, so
# `lead_region()` returns the WHOLE 4,322-byte comment — the strongest real
# case for AC3: not a lead that runs long, a report with no fold at all.
REAL_SPEC_5579228305 = """## Lane 3 Test Spec — a private-repo incident

Derived from the issue's ACs (AC1-AC7), Lane 1's ratified implementation spec (issuecomment-5578766680), and the ready-for-l3's gate scope (issuecomment-5579216165). Write tier R for the wiring/extraction/scheduler logic, testable via mocks. Write tier P, explicitly out of scope for this gate, needs its own AE: the live-write half of `test_h1660_introduction_live.py` (skip-gated behind `HRSE_H1660_WRITE=1`) and the weekly generator's first live cycle.

**MANDATORY constraint, per the active a private-repo incident incident:** do not run `pytest tests` or `pytest backend/tests/integration` unfiltered anywhere in this gate. Use `mise run check` (unit tests only) plus, if a targeted live-marked file needs direct execution, confirm via `--collect-only` first that it deselects correctly before running it alone.

TC1 — **AC1, live-marked, weighted per the original handoff.** The real 2026-08-31 Jed Ayres introduction sentences (the natural-language sentence, not the `INTRO MADE` marker Lane 1's original handoff incorrectly pointed at) produce `INTRODUCED_BY` edges without operator entry, for both Yoni Avital and Marc Potash. Read-only portion only — the write half is tier P, out of scope.
TC2 — **The union-merge with H1683's send-capture, weighted per the ready-for-l3.** An outbound message that is both a draft-send and states an introduction triggers both code paths correctly in the same Gmail delta loop, with neither interfering with the other.
TC3 — **AC5's structural guards, weighted per the ready-for-l3.** Confirm the upstream regex gate, verb-object pairing, and `record_introduction()`'s required provenance fields (`source_activity_id`, `source_quote`, `method`) per the original spec — every written edge must cite these, never inferred from co-occurrence alone.
TC4 — **AC5, negative case.** A meeting Activity with two co-attending Persons and no stated introduction sentence produces no `INTRODUCED_BY` edge.
TC5 — **The root-user-is-one-of-the-two-people bound, weighted per the ready-for-l3.** Confirm Lane 2's added structural bound (beyond the original spec) — the extraction requires the root user to be one of the two people in the introduction, and reason about why this is a legitimate tightening, not an unrequested scope narrowing that would silently miss real introductions between two third parties.
TC6 — **AC2, proactive-task generator bound.** The generator emits exactly 3 tasks per run, drawn only from Persons with a `TRACKING` edge, at least one Activity in the last 90 days, and no existing `INTRODUCED_BY` edge or provenance Task — ordered most-recent-activity-first.
TC7 — **AC3, one person, one action.** A generated task names exactly one Person and is answerable in one action (not "review your relationships").
TC8 — **AC4, idempotent no-re-ask.** Running the generator twice does not re-emit a task for a Person who already has one, via the stable `source_key = "intro-provenance:<person elementId>"` MERGE.
TC9 — **AC4, distinct decline outcomes.** "Nobody" and "don't remember" answers are recorded with distinct `intro_provenance_answer` values (`nobody`/`not_remembered`), neither produces an edge, and the Person is never asked again after either answer.
TC10 — **AC6 — manual entry unchanged.** The frontend Relationships surface can still manually set `INTRODUCED_BY` (post-`INTRODUCED_TO` swap) exactly as it could set `INTRODUCED_TO` before — diff the surface's actual behavior, not just its code.
TC11 — **`INTRODUCED_TO` fully retired.** Grep confirms zero remaining references to `INTRODUCED_TO` in `RelationshipType`, `REL_TYPES`, and `dictationRelationshipTypes.ts` outside historical comments.
TC12 — **AC7 — edge count re-derived live, not hardcoded.** Report the current live `INTRODUCED_BY` count at gate time rather than trusting the stale baseline of 3 (the graph has moved repeatedly this session).
TC13 — **Diff scope.** Re-confirm file count at gate time against the ready-for-l3's stated 18 files.
TC14 — **Standard verification gate, respecting the a private-repo incident restriction.** `mise run check` (unit-scoped, safe); `npm run lint`/`npm run build`; `.venv/bin/mypy app` (cwd=`backend/`); `mise run docs-check`. No unfiltered `pytest tests` or `pytest backend/tests/integration`.
"""

# issuecomment-5579216165 on vitalharmony/a private-repo incident, fetched 2026-09-08.
# Structures itself with THREE `##` sub-headings ("## Lane 1 review",
# "## Gate variant", "## Scope for the gate") — `lead_region()` correctly
# stops at the first of them, 250 bytes in. A real, well-organized
# ready-for-l3 is nowhere near the cap; what it's missing is the two
# required fields, not brevity.
REAL_READY_FOR_L3_5579216165 = """# ready-for-l3 — H1660

**Target:** `l2/h1660-introduced-by` @ `ffa320aa`. PR none yet — pushing before opening one.
**Base:** confirmed current against `origin/main` — ancestry checked immediately before posting, in the same action (R-0338).

## Lane 1 review — verified independently, not accepted on the report

| claim | verification |
|---|---|
| `record_introduction()` refuses an edge lacking `source_activity_id`/`source_quote`/`method` | confirmed directly in `introduction_service.py` |
| The extraction gate requires the root user to be one of the two people | confirmed in `introduction_extraction.py` — Lane 2's own added tightening beyond the original spec, reasoned about below |
| Proactive generator emits exactly 3 tasks, ordered most-recent-activity-first | confirmed in `introduction_provenance_tasks.py` |
| mypy clean | Success, 304 files |
| Diff scope | 18 files, matches |

## The root-user bound — a tightening, not a narrowing

Lane 2 added a bound the original spec did not ask for: the extraction only fires when the root user is one of the two people in the introduction sentence. Reasoned about rather than accepted on the report — this is correct, not scope creep: `capture_from_activity()` runs against the operator's own Gmail/meeting corpus, where every real introduction sentence by construction involves the root user as sender or a stated party. A third-party introduction (two other people introduced to each other, described in an email the operator merely received) is not extractable from a first-person "I want to introduce X to Y" pattern in the same way, and generalizing to it now would be building for a case the live corpus does not evidence yet.

## Gate variant

Standard, **plus a live-marked probe for TC1** (the real Jed Ayres introduction sentences) — read-only portion only, per the handoff's own scope. Write tier P (the live-write half, and the weekly generator's first real cycle) is explicitly out of scope for this gate and needs its own AE once requested.

## Scope for the gate

- **Weight TC1 and TC2 most** — the live extraction proof and the union-merge with H1683's send-capture are the two genuinely new integration surfaces this branch adds.
- **TC5 gets explicit reasoning, not just a pass/fail** — the root-user bound is a real design deviation from the original spec and Lane 3 should say why it holds, not just confirm the code matches what's there.
- Re-derive the live `INTRODUCED_BY` edge count at gate time — the baseline of 3 in the handoff is already stale.
"""

# issuecomment-5579651490 on vitalharmony/a private-repo incident, fetched 2026-09-08.
# Predates `post_lane_discussion.py`'s `kind=gate-result` (harmonic-forge#473)
# and carries no footer to strip. Already names `Finding`/`Next` — real Lane
# 3 practice converged on those independently too — but has no `Verdict`
# line. Its lead (before the `<details>` block, which is where this one
# actually folds) is 1,654 bytes — real, substantive, and correctly under
# the cap: a two-paragraph finding is not the pathological case AC3 exists
# to catch.
REAL_GATE_RESULT_5579651490 = """## Lane 3 Gate Results — a private-repo incident (re-gate after retry fix) — FAIL — TC1 still fails, third consecutive round on the same case/category

**Finding:** Independently reproducing TC1 three separate times, as the AE specified, gives **1 pass / 2 fail** — and both failures are not single-sample noise: each one exhausted the shipped code's own `_LIVE_ATTEMPTS = 3` internal retry (3/3 sub-attempts empty) before failing. This is a genuine, reproducible capability failure by the test's own design intent ("failed all 3 attempts — that is a capability failure, not variance"), not the tolerable variance the retry was added to absorb.
**Next:** This is the third consecutive round where this same test case (`TestReadOnly::test_the_extractor_resolves_all_three_roles_on_real_text`), same fixture, fails to reliably resolve a real introduction — round 1 (parse bug, fixed), round 2 (bare non-determinism), round 3 (this run — non-determinism survives a 3-attempt retry 2 of 3 outer runs). Naming this explicitly for Lane 1 rather than escalating it myself: the pattern now spans 3 rounds in the same structural category (this extraction path's live reliability), which is the sticky-wicket trigger condition — invoking that tool is Lane 1's call, not Lane 3's, per the standing correction on this issue's own thread. Recommend Lane 1 re-open the reforge-vs-retry decision with this new evidence: a 3-attempt retry did not make the outer test reliable, so either the retry budget is too small, the prompt itself needs to change, or the structural parser-replacement reforge already drafted (and held, not reversed) is the right path after all.

<details><summary>Evidence</summary>

Target: `l2/h1660-introduced-by` @ `7770c2292863db8ee211ceb51e24ddfc4a92a6f1`, PR #1696.
AE (issuecomment-5579623056) and gate-readiness sweep (issuecomment-5579627765) confirmed before executing — both authorize write tier R and explicitly instruct three independent outer runs of TC1. a private-repo incident restriction honored: no unfiltered `pytest tests`/`pytest backend/tests/integration`; `--collect-only` first (5 deselected, correctly live-marked), then only `TestReadOnly` run directly, three separate invocations.

### TC1 — AC1, live-marked, weighted most, run 3 independent times per the AE

```
Attempt 1: 4 passed in 5.67s   (all TestReadOnly cases, including the load-bearing one)
Attempt 2: 1 failed, 3 passed in 15.43s
Attempt 3: 1 failed, 3 passed in 13.34s
```

Attempts 2 and 3 failure detail (identical shape both times):
```
Failed: 4:655d4983-3ec6-40fa-9d3a-42909893d030:7102 failed all 3 attempts — that is a capability failure, not variance:
  attempt 1: no introduction resolved from 4:655d4983-...:7102 / assert []
  attempt 2: no introduction resolved from 4:655d4983-...:7102 / assert []
  attempt 3: no introduction resolved from 4:655d4983-...:7102 / assert []
```
No parse-error/warning logged in either failing run — consistent with round 2's finding that the current defect is the model call returning an unusable answer, not a parsing gap. The bounded retry (`_LIVE_ATTEMPTS = 3`, confirmed present at the code level per Lane 1's own AE) did not convert either failing outer run into a pass — all three internal sub-attempts came back empty both times it failed.

### Confirmed no side effect
**PASS-equivalent.** `MATCH ()-[r:INTRODUCED_BY]->() WHERE r.method='extracted' RETURN count(r)` → **0** across all three attempts.

### Not executed
TC1 is the load-bearing case per Lane 1's own explicit weighting, and 2 of 3 independent runs failed it — the remaining TCs (2-14) were not executed this round, consistent with the standing practice on the two prior FAIL rounds for this same issue.

</details>
"""


class RealCorpusDemonstration(unittest.TestCase):
    def test_the_real_spec_has_no_fold_at_all(self) -> None:
        """The strongest real case: not a long lead, a report with no
        `###`/`##`/`<details>` stop anywhere, so the whole comment IS the
        lead."""
        region = L.lead_region(REAL_SPEC_5579228305)
        self.assertEqual(region, REAL_SPEC_5579228305)
        self.assertGreater(len(region.encode()), L.LEAD_CAP_BYTES)

    def test_the_real_spec_is_refused_for_missing_cases_and_next(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            L.validate_lead("spec", REAL_SPEC_5579228305)
        message = str(ctx.exception)
        self.assertIn("`**Cases:** ...`", message)
        self.assertIn("`**Next:** ...`", message)

    def test_the_real_spec_is_also_over_the_cap(self) -> None:
        """Independent of the missing fields: even if both were present,
        4,322 bytes of undifferentiated prose is still over the 4,096-byte
        cap — this is the real artifact that calibrated the cap's floor."""
        padded = REAL_SPEC_5579228305.replace(
            "Derived from",
            "**Cases:** 14 (write tier R).\n**Next:** submit for HITL approval.\n\nDerived from",
            1,
        )
        with self.assertRaises(SystemExit) as ctx:
            L.validate_lead("spec", padded)
        message = str(ctx.exception)
        self.assertIn("byte cap", message)
        self.assertNotIn("`**Cases:** ...`", message)

    def test_the_real_ready_for_l3_folds_correctly_and_is_nowhere_near_the_cap(self) -> None:
        """The bug this file's own first version had: it claimed this body
        had "no `###` heading" and measured 2,825 bytes. It has three `##`
        headings and folds at the first one, 250 bytes in — the fix in
        `lead_region()` (skip the artifact's own opening line, then match
        any heading level) is what makes this correct now."""
        region = L.lead_region(REAL_READY_FOR_L3_5579216165)
        self.assertLess(len(region.encode()), 400)
        self.assertLess(len(region.encode()), L.LEAD_CAP_BYTES)

    def test_the_real_ready_for_l3_is_refused_for_missing_verified_and_next(self) -> None:
        """It already names its `Target` — the real Lane 1 practice this
        session converged on independently — so only `Verified`/`Next` are
        the missing labels. Not a cap failure: this body is well under the
        cap once correctly folded, and stays under it even with both
        fields added (see the next test)."""
        with self.assertRaises(SystemExit) as ctx:
            L.validate_lead("ready-for-l3", REAL_READY_FOR_L3_5579216165)
        message = str(ctx.exception)
        self.assertIn("`**Verified:** ...`", message)
        self.assertIn("`**Next:** ...`", message)
        self.assertNotIn("`**Target:** ...`", message)
        self.assertNotIn("byte cap", message)

    def test_the_real_ready_for_l3_passes_once_the_two_fields_are_added(self) -> None:
        """The positive case this file's first version never actually
        proved: a real, well-organized artifact that's missing only its
        required fields passes cleanly once they're added — the guard
        doesn't demand more than what AC1 asks for."""
        completed = REAL_READY_FOR_L3_5579216165.replace(
            "**Base:**",
            "**Verified:** mypy clean, diff scope matches, root-user bound reasoned about above.\n"
            "**Next:** Lane 3 gates the standard-plus-live-probe spec.\n\n**Base:**",
        )
        L.validate_lead("ready-for-l3", completed)

    def test_the_real_gate_result_has_finding_and_next_but_no_verdict(self) -> None:
        """Real Lane 3 practice already converges on `Finding`/`Next`
        independently of this issue — only `Verdict` is new."""
        with self.assertRaises(SystemExit) as ctx:
            L.validate_lead("gate-result", REAL_GATE_RESULT_5579651490)
        message = str(ctx.exception)
        self.assertIn("`**Verdict:** ...`", message)
        self.assertNotIn("`**Finding:** ...`", message)
        self.assertNotIn("`**Next:** ...`", message)
        self.assertNotIn("byte cap", message)

    def test_the_real_gate_result_lead_is_substantial_but_correctly_under_cap(self) -> None:
        """1,654 bytes for a two-paragraph finding on a third consecutive
        FAIL is real, substantive content — correctly under the 4,096-byte
        cap, not a false negative. The cap exists for the no-fold-at-all
        case (see the spec test above), not to compress a genuine finding."""
        region = L.lead_region(REAL_GATE_RESULT_5579651490)
        self.assertGreater(len(region.encode()), 1000)
        self.assertLess(len(region.encode()), L.LEAD_CAP_BYTES)


if __name__ == "__main__":
    unittest.main()
