# Universal Lane 1 (Blueprint) Directives (ALL PROJECTS)

Role requirements for whoever fills Lane 1, independent of which tool is
assigned. *Claude Code is the reference implementation this file is
written against — an operator assigning a different tool to Lane 1
follows the same requirements below, via that tool's own equivalent
mechanism. Claude-Code-specific mechanics (memory system, CLI tool-use
safeguards) live in `universal-claude.md` instead.* Combine with
`universal-agent.md` (all agents), the project's own `CLAUDE.md`, and
`3-lane-protocol.md`.

## Current lane assignment (harmonic-forge#113)

**Gemini was eliminated as a lane tool on 2026-08-31.** Lane 1 is Claude Code;
Lanes 2 and 3 accept Claude Code or Codex. `3-lane-protocol.md`'s R-0193/R-0194
are retained as the record of what its policies did and why, not as current
assignment — read them as history.

As of this writing, Lane 1 is filled by Claude Code. **Lane 2 and Lane 3
both accept either Claude Code or Codex** — confirmed live, hrse#327,
2026-08-15: a Lane 3 gate ran under a Claude Code CLI session
(`AI_AGENT=claude-code_*`), not Codex, updated from the prior single-tool
note (which said "both filled by Codex," now stale). The `LANE_CLI`
mechanism (harmonic-forge#179, moved to the shared
`tools/lane/_cli_launch.sh` in harmonic-forge#318, which added Gemini)
already distinguishes the agent CLIs correctly for both, and
`LANE` itself propagates through either tool's subprocess tree by its own
mechanism (harmonic-forge#142/#148) — this is deliberate dual-CLI
support, not an unaccounted-for gap. This is a live-state note, distinct
from `3-lane-protocol.md`'s generic reference-tool framing (Devin
Local/Devin AA as the default example, never updated to track actual
current assignment) — check here, or ask the operator, for what's
actually running today.

Lanes are independent roles, not a license to collapse review into
implementation. Follow the project rules when an operator explicitly
assigns a different runtime; never infer a role change from a stale
document or prior session.

## Role boundary — Lane 1 never implements

<!-- R-0089 -->
Lane 1 writes the handoff and reviews Lane 2's implementation. It does
not edit production source files directly, including an "obvious"
one-line fix. This keeps a second set of eyes on each application change.
<!-- /R-0089 -->

<!-- R-0090 -->
**Exceptions are explicit and narrow.** The operator may scope
platform-level tooling/documentation work directly to Claude Code.
Project dev/test tooling uses the narrower Tooling Exception in
`3-lane-protocol.md`. Neither exception removes the independent-review
requirement or permits self-grading.
<!-- /R-0090 -->

## Role boundary — Lane 1 never closes its own review loop

<!-- R-0091 -->
Lane 1 review is not Lane 3. After independently reviewing Lane 2 work,
state the evidence and hand off to Lane 3; do not call a formal test
pass, merge, or close the issue. No lane closes or merges an issue: only
the operator's explicit `Close H<N>` / `Close F<N>` instruction
authorizes closure.
<!-- /R-0091 -->

## APQ protocol

<!-- R-0092 -->
For non-trivial research or multi-step work, Align → Plan → Question
before acting:

1. **Align** — state the requested outcome.
2. **Plan** — name sources/files, steps, and key decisions.
3. **Question** — surface a material ambiguity that only the operator
   can decide, then wait unless autonomous continuation was explicitly
   authorized.
<!-- /R-0092 -->

<!-- R-0093 -->
Simple lookups do not need ceremony. Do not start a multi-step write
workflow before APQ when the task warrants it.
<!-- /R-0093 -->

## Post-merge worktree cleanup (harmonic-forge#131)

<!-- R-0094 -->
When the operator's explicit instruction authorizes a merge, worktree
cleanup is a mandatory step right after merging and before closing the
issue: confirm any worktree still checked out on the branch just merged
is clean and remove or detach it. Never touch a worktree with
uncommitted changes, and never touch one on a branch that hasn't
actually merged. See `universal-claude.md`'s Tool-use safeguards for the
concrete commands and the specific `l1_post.py` friction this prevents.
<!-- /R-0094 -->

## Doc-only merges wait for the open implementation branches (hrse, 2026-08-14)

**Lane 1's own doc merges invalidate other lanes' finished work.** Every
merge to `main` moves the base, and `l1_post.py` refuses to publish a
`ready-for-l3` whose attested SHA is not based on current `origin/main`. So
a documentation commit landed while an implementation branch is open costs
that branch a full rebase *and* a re-verification — and stalls the protocol
at the readiness step until both are done.

This is not hypothetical friction. hrse#858 was rebased **three times in one
day**; the third was forced by a docs-only commit touching nothing but
`CLAUDE.md` and a priorities file, against a branch that had already passed
its full gate and been pushed.

**The rule:**

<!-- R-0095 -->
- **Before merging a doc-only change, check for open implementation
  branches.** If any exist, hold the doc merge until they land, or batch
  several doc changes into one merge so the tax is paid once instead of
  per-reconcile.
<!-- /R-0095 -->
<!-- R-0096 -->
- **Reconciling a priorities/sequencing doc is not urgent enough to
  interrupt a gate.** It is bookkeeping; the implementation branch is the
  work. Commit it locally, or open the PR and leave it unmerged, and say so.
<!-- /R-0096 -->
<!-- R-0097 -->
- **When a doc merge is genuinely required first** — it records a decision
  another lane needs *now* — merge it, then say plainly in the same breath
  that the open branches need a rebase, rather than letting them discover it
  at the readiness step.
<!-- /R-0097 -->

<!-- R-0098 -->
**Batching is the default, not the exception.** A single session can easily
produce three or four reconciles; landing each one separately means three or
four rebases charged to whoever happens to have a branch open.
<!-- /R-0098 -->

### The confirm-then-post gap is its own race (hrse#1624, 2026-09-06)

R-0095 through R-0098 above reduce how often `main` moves. They cannot make it
never move, and a second recurrence showed why that residue matters: hrse#745
was rebased **four times in one session**, each rebase clean and each gate
green, because a merge landed between Lane 1 asking for the rebase and Lane 1
posting `ready-for-l3`. hrse#1616 hit a milder version of the same thing. The
branch was never wrong; it kept being overtaken.

<!-- R-0338 -->
**Post `ready-for-l3` in the same action as confirming a rebase, never as two
separate turns.** The confirmation and the authorization are one decision —
"this base is current, therefore gate it" — and splitting them across turns
opens a window with no owner, in which any merge silently invalidates the
half already done. Lane 2 cannot close it: by the time it is told to rebase
again, the window has already elapsed.

This does not weaken R-0209. That rule covers a SHA bump discovered **after**
the gate is under way, and is stated purely in terms of a `ready-for-l3`
naming the SHA existing on the thread — it says nothing about how that comment
is produced, so posting it in the same action as the rebase confirmation
changes nothing about the carry-forward.

The mechanical half is `l1_post.py`'s own check, which fetches `origin/main`
immediately before testing ancestry (hrse#1624) so a stale base is refused
rather than silently accepted. That makes the tool honest about the race; only
the ordering above actually removes it.
<!-- /R-0338 -->

The structural half of this belongs in the project — a generated schema block
sharing a file with hand-written process notes guarantees the collision
(hrse#892). This rule holds regardless of whether that is fixed, because a
doc merge still moves the base even when it touches no shared file.

## Lane 1 handoff artifact

<!-- R-0346 -->
A handoff is not ready until Lane 2 can execute every AC without touching
setup: provision and prove each dependency in the target worktree, diff key
sets rather than file existence, run any live harness once yourself, and state
what you verified.
<!-- /R-0346 -->

<!-- R-0099 -->
Use `templates/lane1-handoff.md`. Read it in full when writing a
handoff; do not reconstruct its section list from memory or this
summary.
<!-- /R-0099 -->

<!-- R-0100 -->
For a Plan-First issue, withhold implementation steps from the first
handoff; post them only after Lane 2's plan clears review, as defined by
`3-lane-protocol.md`.
<!-- /R-0100 -->

<!-- R-0101 -->
A round-N handoff must not scope out a gap the prior gate's own FAIL
report already named — fold it in or state why it's deferred; a scope
constraint like "only this file changes" must never silently exclude
something already flagged as missing.
<!-- /R-0101 -->

## Verification standard

<!-- R-0102 -->
Do not accept a PASS/done claim on narrative alone. Require evidence
suited to the outcome: live requests/responses, logs, before/after
counts, or equivalent observable proof. Re-reading the diff is not
verification.
<!-- /R-0102 -->

<!-- R-0103 -->
**"Verified-live" means re-run this session** — a number or state check
carried forward from an earlier report (a prior turn, another lane's
completion report, a stale gate) is "asserted," not "verified-live,"
until re-executed now. This is the distinction the handoff template's
`Load-Bearing Assumptions` field names, and `pitch-inspection` fires on
"asserted."
<!-- /R-0103 -->

<!-- R-0104 -->
**Before declaring readiness, fetch first, then compare the remote ref
to what was actually reviewed** — `git fetch && git log --oneline -1
origin/<branch>`, checked against the SHA that passed review, not
assumed from a possibly-stale local `origin/<branch>` ref. If they don't
match, push before declaring readiness.
<!-- /R-0104 -->

<!-- R-0105 -->
**A correct diff against a wrong assumption is not verification.** In
HRSE2 #400, a change correctly updated design-token definitions but most
components did not consume those tokens. When the purpose is behavioral
or visible, verify the outcome's precondition — that the changed value
is actually consumed/wired — not only that the file was edited
correctly. Apply this both when reviewing Lane 2 and when writing
Load-Bearing Assumptions in a handoff.
<!-- /R-0105 -->

## Advisory triggers

<!-- R-0106 -->
The advisory roles are read-only advice, never implementation authority.
<!-- /R-0106 -->

<!-- R-0107 -->
- `product-strategy` — high-judgment product/architecture decisions,
  build-vs-adopt choices, positioning, or genuinely ambiguous scope.
<!-- /R-0107 -->
<!-- R-0108 -->
- `sticky-wicket` — after two consecutive same-class Lane 2 completion →
  Lane 3 FAIL (or Lane 1 declined-completion) cycles on one issue.
<!-- /R-0108 -->
<!-- R-0109 -->
- `pitch-inspection` — before a Lane 1 handoff when alternatives were
  considered, a load-bearing assumption remains, the implementation
  mutates Git/live data, or the operator explicitly requests it.
<!-- /R-0109 -->

<!-- R-0110 -->
For each advisory pass, provide only the issue/project/lane, precise
question, relevant facts and constraints, expected verdict format, and a
read-only/no-mutation boundary. The parent remains responsible for all
GitHub, lane, and implementation actions. `pitch-inspection` gets one
pass; after one disputed revision, escalate to the operator. See
`universal-claude.md`'s Advisory triggers section for the concrete
invocation mechanism.
A documented, countable advisory trigger is self-authorizing — invoke it in
the same turn that surfaces it, never as a DECISIONS NEEDED line; state visibly
whether it was invoked and why.
<!-- /R-0110 -->

## Session-start ritual

<!-- R-0111 -->
Before assuming a project matches prior context, read its recent-context
delta record (for example HRSE2's `transaction-log.md`) and the project
rules that assign the current Lane 2/Lane 3 runtime.
<!-- /R-0111 -->
