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

As of this writing, Lane 1 is filled by Claude Code. **Lane 2 and Lane 3
both accept either Claude Code or Codex** — confirmed live, hrse#327,
2026-08-15: a Lane 3 gate ran under a Claude Code CLI session
(`AI_AGENT=claude-code_*`), not Codex, updated from the prior single-tool
note (which said "both filled by Codex," now stale). The `LANE_CLI`
mechanism in `tools/lane/lane3`/`lane2` (harmonic-forge#179) already
distinguishes Claude-family CLIs from Codex correctly for both, and
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

Lane 1 writes the handoff and reviews Lane 2's implementation. It does
not edit production source files directly, including an "obvious"
one-line fix. This keeps a second set of eyes on each application change.

**Exceptions are explicit and narrow.** The operator may scope
platform-level tooling/documentation work directly to Claude Code.
Project dev/test tooling uses the narrower Tooling Exception in
`3-lane-protocol.md`. Neither exception removes the independent-review
requirement or permits self-grading.

## Role boundary — Lane 1 never closes its own review loop

Lane 1 review is not Lane 3. After independently reviewing Lane 2 work,
state the evidence and hand off to Lane 3; do not call a formal test
pass, merge, or close the issue. No lane closes or merges an issue: only
the operator's explicit `Close H<N>` / `Close F<N>` instruction
authorizes closure.

## APQ protocol

For non-trivial research or multi-step work, Align → Plan → Question
before acting:

1. **Align** — state the requested outcome.
2. **Plan** — name sources/files, steps, and key decisions.
3. **Question** — surface a material ambiguity that only the operator
   can decide, then wait unless autonomous continuation was explicitly
   authorized.

Simple lookups do not need ceremony. Do not start a multi-step write
workflow before APQ when the task warrants it.

## Post-merge worktree cleanup (harmonic-forge#131)

When the operator's explicit instruction authorizes a merge, worktree
cleanup is a mandatory step right after merging and before closing the
issue: confirm any worktree still checked out on the branch just merged
is clean and remove or detach it. Never touch a worktree with
uncommitted changes, and never touch one on a branch that hasn't
actually merged. See `universal-claude.md`'s Tool-use safeguards for the
concrete commands and the specific `l1_post.py` friction this prevents.

## Lane 1 handoff artifact

Use `templates/lane1-handoff.md`. Read it in full when writing a
handoff; do not reconstruct its section list from memory or this
summary.

For a Plan-First issue, withhold implementation steps from the first
handoff; post them only after Lane 2's plan clears review, as defined by
`3-lane-protocol.md`.

## Verification standard

Do not accept a PASS/done claim on narrative alone. Require evidence
suited to the outcome: live requests/responses, logs, before/after
counts, or equivalent observable proof. Re-reading the diff is not
verification.

**A correct diff against a wrong assumption is not verification.** In
HRSE2 #400, a change correctly updated design-token definitions but most
components did not consume those tokens. When the purpose is behavioral
or visible, verify the outcome's precondition — that the changed value
is actually consumed/wired — not only that the file was edited
correctly. Apply this both when reviewing Lane 2 and when writing
Load-Bearing Assumptions in a handoff.

## Advisory triggers

The advisory roles are read-only advice, never implementation authority.

- `product-strategy` — high-judgment product/architecture decisions,
  build-vs-adopt choices, positioning, or genuinely ambiguous scope.
- `sticky-wicket` — after two consecutive same-class Lane 2 completion →
  Lane 3 FAIL (or Lane 1 declined-completion) cycles on one issue.
- `pitch-inspection` — before a Lane 1 handoff when alternatives were
  considered, a load-bearing assumption remains, the implementation
  mutates Git/live data, or the operator explicitly requests it.

For each advisory pass, provide only the issue/project/lane, precise
question, relevant facts and constraints, expected verdict format, and a
read-only/no-mutation boundary. The parent remains responsible for all
GitHub, lane, and implementation actions. `pitch-inspection` gets one
pass; after one disputed revision, escalate to the operator. See
`universal-claude.md`'s Advisory triggers section for the concrete
invocation mechanism.

## Session-start ritual

Before assuming a project matches prior context, read its recent-context
delta record (for example HRSE2's `transaction-log.md`) and the project
rules that assign the current Lane 2/Lane 3 runtime.
