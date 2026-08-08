# ADR-002: Dev/test tooling gets a lighter exception to the full 3-lane loop

**Date:** 2026-07-12
**Status:** Accepted
**Decider:** Marc Mangus (platform owner)
**Amends:** `3-lane-protocol.md` (new "Tooling Exception" section, `sticky-wicket` trigger lowered 3→2)

## Context

HRSE2 issue #233 — writing a parity-test suite for the `hrse_manager.py` →
mise/process-compose cutover (#224) — went through roughly 10 rounds of
Lane 2 completion claim → Lane 3 gate FAIL (or Lane 1 declining a
completion claim) before landing. It burned a full day's Devin credit
budget, including dipping into a small paid-credit pool, on what is
fundamentally a single test-tooling script with no production blast
radius.

## What actually happened (condensed timeline)

1. **Rounds 1–5:** the same root cause recurring in different clothes —
   `disposable_branch()`'s cleanup (branch-switch-in-place, `check=False`,
   no verification) silently stranded the working tree on a throwaway
   branch under multiple failure modes (an in-process `git reset --hard`,
   a `git stash` collision eating Lane 2's own fixes, a SIGKILL from the
   suite's own per-test timeout). By the time this was named, **nine**
   stray `parity-test/*` branches had accumulated.
2. **The `sticky-wicket` circuit breaker fired** (first real use of the
   mechanism defined the same night) and correctly diagnosed the
   structural problem — but its *first* invocation was actually a
   self-assessment by the reviewing Lane 1 session, not the real
   subagent (a harness limitation: newly-created custom subagents aren't
   available mid-session). That self-assessment recommended a git-worktree
   reforge.
3. **The worktree reforge was wrong**, and only caught because Marc asked
   directly whether the real `sticky-wicket` had actually been invoked.
   Once genuinely invoked (after a session restart made the agent
   available), it found the disqualifying flaw the self-assessment missed:
   `hrse_manager.py` hardcodes its repo path and `cd`s there for every git
   operation, so a worktree copy still commits in the main tree regardless
   of invocation directory. **This is the maker-grades-own-work failure
   mode, observed directly**: the same session that wrote the diagnosis
   also "independently" verified it, and got it wrong.
4. **Rounds 6–9:** Lane 2 repeatedly reported completion that didn't match
   reality — a "reimplemented" comment with zero code changes; a "complete"
   report while none of the work existed on `main`; a pass/skip miscount
   from checking only exit code 0. Each was caught by Lane 1 re-verifying
   live rather than trusting the claim (per the platform's own
   verify-live-not-source standard) — correctly, but at the cost of a full
   review cycle each time.
5. **Marc stopped Lane 2** (cost) and asked Lane 1 to verify directly.
   **Lane 1 then repeated the exact failure mode from step 3**: found a
   real regression, fixed it directly, and was about to run the suite
   itself to verify its own fix — maker grading its own work again, one
   layer up. Marc caught this explicitly ("I'm dubious about this
   approach") before execution.
6. **The real `sticky-wicket` was invoked** (properly, this time, on a
   fresh resumable instance) with two questions: is the just-applied fix
   actually correct, and is the *process itself* the problem. Verdict on
   both: **the fix was wrong** (it reverted two commits that were
   correctly solving a real constraint — `hrse_manager.py` only commits,
   and only appends a transaction-log entry, when the tree is dirty; a
   gitignored/out-of-repo marker file can never trigger that path — and
   the constraint had never been written down, so it kept getting
   "cleaned up" by well-intentioned agents who didn't know it existed).
   **And the process was the dominant cost driver**, independent of the
   bugs: a 12-script suite with full service-restart cycles, disposable
   git branches, and a "passes twice consecutively" idempotency bar,
   routed through the full 3-lane loop, for a script that will run maybe
   three more times ever (at the #235/#236/#237 cutover gates) and never
   ships.

## Decision

Two changes, both directly from the `sticky-wicket` analysis in step 6:

1. **`#233` itself is reforged** from a durable, re-runnable CI suite into
   a **capture-and-diff harness**: run once now (supervised) against
   `hrse_manager.py`, capture normalized golden output, diff against the
   replacement's output at cutover time. "Passes twice consecutively" is
   dropped — it was a CI property a one-time migration oracle never
   needed, and it was responsible for a meaningful share of the git-safety
   engineering that produced rounds 1–5.
2. **A new "Tooling Exception" in `3-lane-protocol.md`**: dev/test tooling
   and repo-local automation — never application code, never shipped, no
   production blast radius — skips the full Lane 1 → Lane 2 → Lane 3 loop.
   Single implementer, one human-reviewed pass, Lane 3 gates only at the
   tooling's first consequential use (for #224, that's the compare-mode
   run at #235/#236/#237), not on every authoring round. The exception is
   explicit and per-issue, modeled on the existing platform-tooling
   exception in `rules/universal-lane1.md`, and does not default open.
3. **The `sticky-wicket` auto-trigger is lowered from 3 to 2 consecutive
   FAIL/declined-completion rounds** on the same issue — by round 3 in
   #233 the pattern was already fully visible in hindsight; firing one
   round earlier costs little when the pattern is real and saves a full
   gate cycle when it is.
4. **A written rule, inside the Tooling Exception itself, that the
   implementer never grades its own verification** — even under the
   lighter process. This is not new to the platform (it's the whole
   premise of Lane 3's independence), but it needed to be said explicitly
   at the single-implementer scale, because both self-assessment failures
   in this incident (steps 3 and 5) happened *inside* what was meant to be
   a shortcut, not inside the full loop.

## Why this is the right call, not just relief from tonight's pain

The full loop's cost is proportional to the number of review rounds, and
the number of rounds for tooling authoring is dominated by ordinary
iteration on a script — not by the risk the loop exists to manage (a bad
change reaching users). Routing #233-shaped work through the same process
as application code doesn't buy more safety, it buys more rounds at the
same safety level a lighter, well-scoped process already provides. The
boundary conditions in the new section (never application source, never a
merge-gating CI path, never secrets, explicit per-issue operator scoping)
exist specifically so this can't quietly widen into skipping review on
things that actually ship.

## Consequences

- `#233` closes tonight under the reforged contract; `#235`/`#236`/`#237`
  use `compare-baseline.sh compare` as their acceptance gate rather than
  re-running the harness's own authoring loop.
- Future project-level dev-tooling issues should be labeled and scoped
  per the new section's boundary at filing time, not decided ad hoc mid-
  issue.
- `sticky-wicket`'s lowered threshold applies platform-wide, not just to
  HRSE2 — it's a change to the shared protocol.
- Open question, not resolved here: should the harness-only "single
  implementer never grades own work" rule extend into a standing
  requirement that *any* solo fix, even outside the tooling exception, get
  a second read before the fixing session verifies it? Noted but not
  decided — a candidate for its own future ADR if the pattern recurs
  outside tooling contexts.
