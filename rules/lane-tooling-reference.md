# Lane tooling reference — `lane_state.py`'s derived states

Reference for whoever works on `lane_state.py` or `l1_post.py`'s footers.
Moved out of `rules/lane-shorthand.md` (harmonic-forge#909) to keep the always-loaded context under its ceiling.
Deliberately **not** in `sync_rules.py`'s `UNIVERSAL_RULE_FILES`, so no session
loads it; it lives under `rules/` so `check_rule_drift.py` keeps checking its
R-ids. The rules a lane must follow unconsulted stayed in `lane-shorthand.md`.

## Derived lane states — `lane_state.py`'s vocabulary

Not operator shorthand. These are the states HRSE2's
`.claude/skills/sprint-plan/scripts/lane_state.py` **derives** from an issue's
posted comments, and they live here for the same reason the tokens above do:
one canonical table, because two copies is how the vocabulary drifted.

Every state carries a **stable machine key** alongside the display string
(hrse#1590). Consumers switch on the key; the key never contains an issue
number or prose, so the display string stays free to be reworded.

| Key | Display string | Meaning |
|---|---|---|
| `blocked.no-tier` | `blocked: no Tier` | the board `Tier` field is unset, so `l1_post.py` will refuse the handoff |
| `blocked.dependency` | `blocked on <target>` | an open dependency |
| `blocked.lane` | `blocked: L<N>B, see thread` | a lane reported BLOCKED |
| `no-handoff` | `no handoff` | nothing posted yet |
| `handoff.posted` | — | timeline-only; folds into a `ready.*` state |
| `ready.plan` | `ready: Plan H<N>` | Plan-First declared; Lane 2 owes a plan |
| `ready.implement` | `ready: Implement H<N>` | Lane 2 owes the implementation |
| `plan.posted` | `plan posted, awaiting L1` | Lane 1 owes a ratification |
| `spec.posted` | `spec posted, awaiting L1` | Lane 3's test spec is up, awaiting approval |
| `implemented.awaiting-gate` | `implemented, awaiting gate` | Lane 2 done, unauthorized |
| `gate.ae-without-sweep` | `AE posted, L1 owes the sweep` | **R-0208 partial transition** — see below |
| `gate.executable` | `authorized, gate executable` | AE + sweep, or that pair carried forward (R-0209) |
| `gate.pass` | `gated` | Lane 3 passed |
| `gate.fail` | `FAIL, back to L2 → Fix H<N>` | Lane 3 failed |
| `l1.rework` | `L1 asked for rework, back to L2 → Fix H<N>` | Lane 1 asked Lane 2 for more work on the branch — a rebase, a correction. **Same trigger as `gate.fail`, deliberately; different key** (hrse#1609) |
| `unknown` | `unknown` | the fail-loud default |

<!-- R-0331 -->
`gate.ae-without-sweep` is an **abnormal partial transition, not a waypoint.**
R-0208 makes the AE and its gate-readiness sweep one atomic action — same
turn, sweep strictly after — so an AE standing alone means the pair did not
complete. The sweep is **Lane 1's** to post (`testing-gate.md` rule 3), and
the state names Lane 1 as the owner because R-0128 exists precisely because
that step was being skipped silently.
<!-- /R-0331 -->

<!-- R-0332 -->
`gate.executable` covers both the fresh AE+sweep pair **and** the same
authorization carried forward onto a new SHA by a `ready-for-l3` after a FAIL
(R-0209, implemented in `check_lane3_ready.py`'s `carry_forward()`). A state
model that demands a fresh AE after every FAIL marks correctly-authorized work
as blocked on the most common cycle in the protocol.
<!-- /R-0332 -->

<!-- R-0333 -->
Authority is the `l1-post` **footer**, never a heading. `kind=` is stamped by
`l1_post.py` only after it has validated the body; a heading is prose anyone
can type into any comment, and the protocol already warns that an AE posted
through ordinary discussion "reads correctly to a human but is invisible to
Lane 3's own spec/AE fetch". Headings are a cross-check: where a footer's
mandated heading is missing, the transition is still derived but marked
`validated=False`, so the disagreement is **reported, not silently resolved.**

`l2_post.py` now covers `L2S`/`L2D`/`L2B`/the `finding` kind, and every kind
it posts carries the same footer, `posted-by=LANE2` included (harmonic-forge
#583) — this paragraph previously said `l2_post.py` covered only
`L2P`/`L2D`/`L2B` with no footer at all, which stopped being true the moment
#583 landed and was corrected here rather than left for the next reader to
trust and build heading-only detection against, again.

The Lane 3 Test Spec and Lane 3 Gate Results are unaffected by #583 (a
different tool, out of that issue's scope) and are not re-verified by this
edit — treat their footer status here as unconfirmed rather than assume it
tracks this paragraph's l2_post.py correction.
<!-- /R-0333 -->

<!-- R-0334 -->
A marker **quoted as evidence never counts as a transition.** Fenced blocks
are stripped before any marker is read — footers included, since `l1_post.py`
never emits one inside a fence. Found live: hrse#1590's own plan comment
tabulates another issue's footers as evidence, and reading the raw body put a
`gate.fail` on a thread that had never been gated.
<!-- /R-0334 -->

## `kind=rework` — why a request for work is not a discussion

<!-- R-0337 -->
**A Lane 1 comment asking Lane 2 for more work on an existing branch is posted
with `--kind rework`, never as ordinary discussion.** `_Markers.newest()`
excludes `discussion` by design — on its own it is Lane 1 talking, not a lane
transition — so a rebase request posted that way left the row still naming
Lane 3 while Lane 2 owed the work and nobody had been told (hrse#1609; observed
live on hrse#1578 and hrse#1606).

**Inferring it from a discussion instead was measured and rejected.** Across
every thread on `vitalharmony/hrse`, 63 have a post-`l2.done` discussion as
their newest marker, and **none of them is a request for work** — they are
closing notes, merge confirmations and gate sign-offs. Treating a newer
discussion as "Lane 2 owes work" would have been wrong 63 times out of 63 on a
model whose contract is fail-loud.

The trigger it renders is `Fix H<N>` — the same one a gate FAIL renders,
because what Lane 2 must do is identical: re-read the issue and do the work on
the branch. Only the operator-facing token is shared; the key stays
`l1.rework`, so the row still says which of the two it is.
<!-- /R-0337 -->
