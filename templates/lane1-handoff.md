<!-- Lane 1 → Lane 2 handoff artifact. Copy this structure for every handoff. -->

## Handoff: [Issue #N — Short Title]

### Issue
- GitHub: {url}
- Labels: {labels}
- Acceptance criteria: reference the issue body by default — Lane 2 fetches
  it directly (`gh api repos/OWNER/REPO/issues/N --jq .body`, REST — see
  harmonic-forge#220). Quote a specific line verbatim only when
  this handoff's spec depends on exact wording (e.g. disambiguating a
  criterion that reads differently than Lane 1's diagnosis) — otherwise a
  copy silently drifts if the issue is edited after handoff.

### Lane 3 Gate Variant
{standard (`rules/testing-gate.md`) | UI golden path
(`rules/frontend-ui-golden-path.md`) | project-specific skill, e.g. HRSE2's
`.devin/skills/lane3-gate/SKILL.md`} — name it explicitly so Lane 3 doesn't
have to infer which gate applies.

### Affected Files
| File | Lines | Change type |
|---|---|---|
| path/to/file.py | 45–78 | Modify — replace X with Y |
| path/to/other.py | new | Create — new service module |

### Root Cause / Entry Point
> "{quoted line or condition that is the root cause}"

### Design Alternatives Considered
{none | list each plausible design that was weighed and why it was rejected
in favor of the chosen one} — "none" means there was one obvious design; a
non-"none" answer is a `pitch-inspection` trigger (see
`3-lane-protocol.md` § Pre-Flight Second Read), not a formality to fill in
after the fact.

### Load-Bearing Assumptions
{none | list each assumption about existing behavior this spec depends on,
each marked **verified-live** (checked against the actual running
code/system by *this session*, cite how — a number or state check carried
forward from an earlier report doesn't count, see `universal-lane1.md`
§ Verification standard) or **asserted** (believed true, not yet
checked)} — any assumption left **asserted** is a `pitch-inspection`
trigger. An assumption that turns out wrong invalidates everything built
on it; naming it here is what makes it checkable instead of silently
inherited.

### Delegated Judgment Calls
{none | list each design decision this spec deliberately leaves to Lane 2
rather than resolving here} — "none" is the common, zero-cost answer. A
non-"none" answer is a **Plan-First trigger** (see `3-lane-protocol.md`
§ Plan-First Implementation): Lane 2 must post its resolution as a plan and
get it reviewed before writing any code. Don't write "none" here because a
decision is inconvenient to make now — an undeclared delegation is exactly
the gap that produced HRSE2 #233's recurring bug class.

### Pre-Flight Preconditions
{none | for a live/`--apply`/data-mutating command or a cross-repo
deliverable, list every precondition the receiving action will hit, each
marked **traced** (target's guard-clause/required-flag chain read, evidence
pasted inline), **verified-present** (confirmed live in the *receiving*
environment — tool installed, worktree trusted, sibling path exists), or
**external-blocked** (genuinely unresolvable by either lane, e.g. a live
console value only a human can check — name it, don't guess it)} — see
`3-lane-protocol.md` § Pre-Handoff Precondition Trace. A blank/incomplete
field on a qualifying handoff blocks posting, same as an "asserted"
Load-Bearing Assumption. Lane 2 does not trust this field on faith — it
re-verifies each item itself before implementing (same section).

### Implementation Spec
**If this handoff triggers Plan-First Implementation** (`3-lane-protocol.md`
§ Plan-First Implementation — Delegated Judgment Calls above is non-"none",
the work mutates git/live data, or HITL said "Plan-first #N"): **omit this
section from the initial post.** Post everything above as the handoff,
stop there, and wait for HITL to relay "Plan #N" (not "Implement #N" — see
`3-lane-protocol.md` § HITL Gate Language step 2a). Once Lane 2's plan
draws a PROCEED/PROCEED WITH NAMED CHANGES verdict, post this section as a
**follow-up comment** on the same issue, incorporating any named changes,
and only then does HITL send "Implement #N". This is a hard split, not a
formality — per ADR-005 (HRSE2 #236), co-delivering the spec with a "plan
first" instruction in the same comment is what let the gate be skipped
even when explicitly, repeatedly stated. Otherwise (the common case, no
Plan-First trigger): {explicit step-by-step instruction for Lane 2 — no
ambiguity}

**Long-running script requirement:** if the implementation will make more
than 50 sequential network calls, or one full run may outlive a Lane 3
execution turn, this spec must require (1) incremental checkpointing plus
safe resume, (2) a bounded-work control such as `--limit N`, and (3) a
network-free report mode that reads local checkpoints/results. Include one
concrete test case for each. A terminal-only evidence artifact, progress logs,
or a longer timeout is not a substitute. See `3-lane-protocol.md`
§ Long-Running Script Handoffs.

**No secrets in this handoff.** Name the env var a step depends on, never
its value — this handoff is posted as a permanent comment on the GitHub
issue, a wider audience than a local file. **Same applies to sensitive
real-world data that isn't a credential** (negotiation figures, PII,
contact details) — see `rules/universal-agent.md` § SECURITY → "Passing
sensitive real-world data between lanes" for the gitignored-local-file +
path-only-comment pattern.

### Test Cases (for Lane 3)
1. After fixing, [action] must produce [Y] and must NOT produce [Z].
2. ...

### Read-Before-Edit Instruction
Read the cited lines and quote the root-cause line or condition before
making any change.

### Ambiguity Gate
If any instruction above is unclear, Lane 2 must stop and escalate to the
Tech Lead before making any change.
