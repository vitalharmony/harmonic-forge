# ADR-003: pitch-inspection — a narrow, self-declaring pre-flight second read

**Date:** 2026-07-12
**Status:** Accepted
**Decider:** Marc Mangus (platform owner)
**Resolves:** the open question left at the end of ADR-002 ("should the
harness-only never-grade-your-own-work rule extend into a standing
requirement that any solo fix get a second read before the fixing session
verifies it?")
**Amends:** `3-lane-protocol.md` (new "Pre-Flight Second Read" section),
`templates/lane1-handoff.md` (two new mandatory fields), adds
`agents/pitch-inspection.md`

## Context

ADR-002 documented HRSE2 #233's ~10-round thrashing incident and the
Tooling Exception + `sticky-wicket` 2-round trigger it produced. Both of
those are reactive — they fire after a problem has already surfaced across
multiple rounds. The same incident also contained a *design-level* failure
mode, observed twice in one night: Lane 1 wrote a design or a fix, then
was the one who "independently" verified it, and got it wrong both times
(the doomed git-worktree reforge, and a fix that reverted two correct
commits). Both were caught only because the human operator asked directly
whether independent review had actually happened — not because any rule
required it.

Marc asked whether a proactive mechanism — invoked *before* Lane 2 starts
implementing a high-risk/complex issue — was worth building, to
short-circuit thrashing at its source rather than catch it after 2 rounds.

## Evaluation (via `product-strategy`, 2026-07-12)

The broad framing — a standing gate on "high-risk/complexity issues" —
was evaluated and **rejected**: that trigger is not checkable at issue-
filing time without becoming a vibe, and a mandatory gate on every
"complex" issue would tax a large population of issues that were going to
go fine, more than thrashing itself has cost. The tooling-specific flagship
case (#233) is also already double-covered by the Tooling Exception's
human-reviewed pass and by `sticky-wicket`'s lowered 2-round trigger — a
third mechanism aimed at the same incident would be redundant there.

The evaluation also gave an honest partial-credit read: a pre-flight
review, had it existed, would plausibly have caught #233's design-level
failures (the `disposable_branch` cleanup design, and the unwritten
constraint about when `hrse_manager.py` commits) but would **not** have
touched rounds 6–9, which were false-completion-report failures — a
verification-honesty problem, not a design problem, already governed by
the platform's verify-live-not-source standard. Roughly half the
incident's cost, not all of it.

The case *for* a narrower version rests on different evidence than #233
itself: the maker-grades-own-design failure happened **twice in one
night, independent of the tooling incident** — Lane 1's self-assessment
standing in for the real `sticky-wicket` subagent, then Lane 1 nearly
verifying its own regression fix. That is the platform's strongest
empirical signal, outside the thrashing pattern, that a design or a fix
authored and graded by the same session is unreliable under exactly the
conditions (a contested design, an unverified assumption, time/cost
pressure) that make catching it valuable.

## Decision

Build the narrow version: **`pitch-inspection`**, a self-declaring,
one-pass pre-flight second read, not a standing risk gate.

- **Trigger is self-declared, not separately assessed.** Two new mandatory
  fields in `templates/lane1-handoff.md` — *Design Alternatives
  Considered* and *Load-Bearing Assumptions* (each entry marked
  verified-live or asserted). A non-"none" design-alternatives answer, or
  any "asserted" assumption, fires the review. Most handoffs cost nothing
  beyond writing "none" in two fields.
- **A third, narrower trigger**: the implementation's own operation
  mutates git/live state, and the issue is *not* already routed through
  the Tooling Exception (which already carries its own human-reviewed
  pass for that class).
- **One pass, no iteration loop.** PROCEED / PROCEED WITH NAMED CHANGES /
  REFORGE BEFORE HANDOFF. Disagreement after one revision escalates to
  the human operator — the mechanism does not get to thrash with itself.
- **Fable, read-only, advisory-only** — same shape as `sticky-wicket`
  (`Read, Grep, Glob, WebSearch, WebFetch, Bash`, all read-only survey;
  never mutates).
- **Explicitly does not cover verification-honesty failures.** Documented
  in the agent file itself so a PROCEED verdict is never mistaken for
  assurance about how faithfully later completion claims will match
  reality.

## Amendment (harmonic-forge#838, 2026-09-30): every Tooling Exception handoff

The third trigger's exclusion is removed, and a fourth trigger is added:
every handoff on an issue labeled `tooling-exception` is reviewed before
posting. `l1_post.py` enforces it through a per-issue receipt. The operator
adopted this on 2026-09-30: "the goal is to get these autonomous passes to
go through as few iterations as possible."

**The evidence.** harmonic-forge#834 (standard, 5-refuter panels) found 5
distinct defects in preclose pass 1 and 3 more in pass 2, 2 of them created
by pass 1's fixes, and ended at a sticky-wicket PATCH. harmonic-forge#836
(fast, one refuter) found 2 and then 2 more and ended at sticky-wicket. **9
of those 12 defects came from the Lane 1 handoff**, and none of them was an
assumption the handoff stated. They were unstated: a reader it did not
list, a guard the repo already had, a reset path with no authorizer.

**The three reasons this ADR rejected a broader trigger, answered:**
1. *"High-risk" is not checkable.* The new trigger is a label, which is
   mechanical.
2. *It taxes issues that would have gone fine.* That is real. 156 Tooling
   Exception issues closed in 2026-09 (harmonic-forge 122, hrse 34), and
   each now pays one review. `tools/gh/preclose_pass_report.py` measures
   passes per issue before and after, rather than asserting a saving.
3. *The Tooling Exception is already covered by its own reviewed pass and
   by `sticky-wicket`.* Both run after implementation. The #834 and #836
   data is those two mechanisms looping on defects a pre-implementation
   read would have caught.

R-0239's one pass, no loop, is unchanged.

## Why this is the right scope, not just a smaller version of the same idea

The two new handoff fields have value independent of the agent ever
running: forcing load-bearing assumptions to be written down and marked
verified/asserted is itself most of what would have surfaced #233's
unwritten constraint, whether or not a reviewer reads the field. The
agent is cheap to fire (one Claude-side subagent call inside Lane 1's own
session, no Devin credits, no human-gate round) and self-limiting (it only
fires when Lane 1 itself flags something contestable), so the expected
cost is low while the population it targets — contested designs and
unverified assumptions — is exactly where the platform's one documented
design-level failure mode actually lives.

## Consequences

- Every future Lane 1 handoff carries the two new fields; "none" is a
  valid, zero-cost answer for the common case.
- `pitch-inspection` symlinked into HRSE2's `.claude/agents/` alongside
  `sticky-wicket` and `product-strategy`, same manual-symlink pattern
  (proper distribution via `sync_rules.py` is tracked separately in
  harmonic-forge#47 — not blocking).
- This resolves ADR-002's open question for the design-time case
  specifically. It does not extend to routine implementation work with an
  obvious design — that remains Lane 1's ordinary job, unreviewed by this
  mechanism, exactly as it was before tonight.
