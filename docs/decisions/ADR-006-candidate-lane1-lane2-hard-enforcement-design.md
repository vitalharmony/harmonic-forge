# ADR-006 (candidate): Hard mechanical enforcement for Lane 1/Lane 2 constraints per tool

**Date:** 2026-08-07
**Status:** Proposed (design candidate, per harmonic-forge#115 — not yet
accepted; no implementation issue filed until this is reviewed)
**Decider:** Marc Mangus (platform owner) — pending
**Relates to:** harmonic-forge#111 (Lane symmetry epic), #115 (this spike),
#142/#149/#150/#151 (the already-shipped Lane 2/3 hard-enforcement work
this design extends), #152 (Codex-side LANE-conditional git-mutation deny)
**Investigation, not implementation** — per harmonic-forge#115's scope, no
code changes accompany this document.

## Context

Lane 3's "never fixes anything" rule is backed by hard tool-level
enforcement — a `PreToolUse` hook (`block_lane1_status_claims.py`'s
`lane3_write_outside_testplan()`) that mechanically denies `Edit`/`Write`
outside `~/Harmonic_Projects/testplan/` when `LANE=3`, plus (for Codex)
`gate_codex_tool.py`'s `LANE`-conditional git-mutation deny (#152). This
was built specifically because prose-only Lane 3 rules were violated
under pressure repeatedly (hrse#254, #262, #264, #270).

Lane 1's "never implements production source" and Lane 2's "never
executes a data-mutating script's write/apply path" constraints have no
equivalent hard restriction today — both are self-policed prose, the
same failure mode Lane 3's hard gate was built to close. This issue was
rescoped 2026-07-31 (see the issue's own rescope comment) — the original
Lane 2/3 half of the investigation is already answered empirically by
#142/#150/#151, which shipped since the issue was first filed. What
remains is Lane 1's constraint specifically, plus per-tool gap-reporting.

## Investigation

### 1. Claude Code Lane 1 — does `settings.json`'s permission model support this?

**Partially, and not the way the question implies.** `settings.json`'s
native `permissions.allow`/`deny` arrays (confirmed live,
`.claude/settings.local.json` in this project — a large `allow` array of
`Bash(...)`, `Read(...)`, `WebFetch(domain:...)` glob patterns) are
**static glob matches evaluated with no access to the process
environment**. They cannot express "deny `Edit`/`Write` under
`backend/app/` *only when* `LANE=1`, but allow it when the operator has
granted this session the Tooling Exception" — that's a runtime,
env-conditioned decision, not a fixed pattern.

The mechanism that **can** express this is the same one already proven
for Lane 2/3: a `PreToolUse` hook registered in `settings.json`'s `hooks`
block (confirmed live — this project's own `.claude/settings.json`
already registers `block_lane1_status_claims.py` against both the `Bash`
and `Edit|Write` matchers). The hook script itself reads `os.environ.get("LANE")`
at invocation time and returns a structured allow/deny decision — this is
the dynamic layer `permissions.deny`'s static globs can't provide.
**Answer: yes, mechanically enforceable, but through the hook-script
layer `settings.json` already wires in, not through `permissions.deny`
alone.**

### 2. What the Lane 1 hook logic would look like

Symmetric in *mechanism* with the existing `lane2_write_in_main_checkout()`/
`lane3_write_outside_testplan()` guards in `block_lane1_status_claims.py`,
but the **boundary is a genuinely harder problem**:

- Lane 2's guard denies one thing: writes to the main checkout root
  (structurally derivable from `cwd` — `git rev-parse --show-toplevel`,
  strip a `-lane<N>` suffix).
- Lane 3's guard denies everything **except** one thing: writes outside
  `TESTPLAN_ROOT` (also structurally derivable — a fixed, project-agnostic
  path).
- Lane 1's constraint is **content-type-based, not location-based**:
  "never implements production source," except platform-tooling/docs
  work under the explicit, per-issue Tooling Exception (ADR-002). There
  is no single structurally-derivable root that separates "application
  source" from "tooling/docs" the way `TESTPLAN_ROOT` or the main-checkout
  root does — "application source" (`backend/app/`, `frontend/src/`,
  schema/migration files) is a *per-project* set that has to be
  configured, not derived from `cwd` alone. This is the real gap: the
  existing two guards are project-agnostic by construction
  (harmonic-forge#149); a Lane 1 guard needs a **per-project denylist of
  application-source roots**, which is new surface area the canonical
  hook file doesn't have a precedent for yet (it currently has zero
  per-project configuration — everything is derived structurally).

Proposed shape (not implemented — this is the design, pending review):

```python
# Per-project denylist — new kind of config this file doesn't have yet.
# Keyed by resolved project name (same derivation resolve_main_checkout_root
# already uses), values are paths relative to the main checkout root.
APPLICATION_SOURCE_ROOTS: dict[str, list[str]] = {
    "HRSE2": ["backend/app", "frontend/src"],
    # ... other projects added as they adopt this guard
}

def lane1_write_in_application_source(file_path: str, cwd: Path) -> bool:
    if os.environ.get("LANE") != "1":
        return False
    if os.environ.get("LANE1_TOOLING_EXCEPTION") == "1":
        return False  # see Tooling Exception override, below
    # resolve file_path against APPLICATION_SOURCE_ROOTS[project_name]...
```

**Tooling Exception override — explicit env var, not an inferred or
self-armed signal.** Mirrors `LANE` itself: `LANE1_TOOLING_EXCEPTION=1`
set once at session launch by the operator (e.g. a `lane1
--tooling-exception` flag on the launcher, analogous to how `LANE` is set
by the `lane1`/`lane2`/`lane3` scripts), never a marker file the session
can touch mid-session and never inferred from conversation text. This
follows the same reasoning that ruled out a self-armed marker for the
existing `LANE=2`/`LANE=3` guards (harmonic-forge#142's comment history)
— a signal the session itself can set is not a real restriction.

### 3. Non-Claude tool filling Lane 1

**Explicitly out of scope for this pass, not designed.** Per project
memory: Devin is not currently in use for any lane; Codex fills Lane 2/3;
Lane 1 is Claude Code only, today. A Lane 1 reassignment to a non-Claude
tool is not a live scenario. Designing a hard-enforcement mechanism for a
hypothetical tool with unknown permission/hook capabilities would be
speculative — per this issue's own framing ("if so, that gap must be
reported explicitly, not papered over with a prose-only fallback
presented as equivalent"), the honest statement is: **this is a real,
currently-dormant gap, not a solved one.** Revisit if/when Lane 1 is ever
reassigned; do not build for it now.

### 4. Lane 2 hard-restricted profile per tool — "never runs a data-mutating script's write/apply path"

Live Lane 2 tool: **Codex.** This is a different, harder-to-detect
constraint than #152's git-mutation deny (a fixed, small set of `git`
subcommands) — an arbitrary `scripts/*.py` invocation with some
apply/execute/write flag isn't a fixed vocabulary the way `git commit`
is.

Two options considered:
1. **Per-script allowlist/blocklist** — maintain an explicit list of
   known data-mutating scripts and their apply flags. Precise, but
   requires updating the hook every time a new mutating script is added
   — brittle, and silently stale the moment someone forgets.
2. **Generic flag-pattern heuristic** — deny `python3`/`python`
   invocations of anything under `scripts/` when `LANE=2` and any
   argument is in a small denylist of apply-shaped flag names
   (`--apply`, `--execute`, `--write`, `--force`). Matches this
   codebase's own existing posture for these guards: **non-adversarial,
   catches accidental role mix-ups** (explicit precedent:
   `block_lane1_status_claims.py`'s own docstring, "the goal is catching
   an accidental role mix-up... not stopping a deliberately dishonest
   session"). A heuristic with occasional false positives (a legitimate
   read-only script that happens to use `--force` for an unrelated
   reason) is consistent with that existing tolerance; a missed script
   under option 1 is a silent gap, not a false positive — worse failure
   mode for a safety guard.

**Recommendation: option 2**, extending `gate_codex_tool.py`'s
`blocked_reason()` with one more `program`/`args` check, mirroring its
existing shape (`sudo`, `shell -c`, `python -c`, `npm/pip install` are
already denied unconditionally there; this would be a new `LANE`-gated
case alongside the git-mutation check #152 already made `LANE`-gated).

**Symmetric gap on the Claude Code side:** if Claude Code ever fills Lane
2 (not the case today, but the canonical hook file is meant to be
tool-agnostic in principle), `block_lane1_status_claims.py`'s
`lane2_write_in_main_checkout()` only denies `Edit`/`Write` calls — it has
no equivalent `Bash`-tool check for a data-mutating script's apply path.
Worth the same option-2 treatment if/when this becomes live; not
implemented now since Codex is the only live Lane 2 tool.

## Gaps, stated plainly (per this issue's explicit instruction not to paper over them)

1. **Lane 1 hard enforcement does not exist today** — `LANE=1` is set by
   the `lane1` launcher script and read by nothing. This document is a
   design, not a fix; #115 is investigation-only by its own scope.
2. **No project currently has an `APPLICATION_SOURCE_ROOTS`-style config**
   — this is new surface area, not an extension of an existing pattern.
3. **Non-Claude Lane 1 tooling is genuinely undesigned**, not just
   unimplemented — there is no answer yet for what mechanism a
   hypothetical future Lane 1 tool would even offer.
4. **Lane 2's data-mutating-script constraint has no tool-specific
   enforcement for Codex or Claude Code today** — #152 only covers `git`
   subcommands, not arbitrary scripts.

None of these are contradicted by any currently-live tool having *zero*
enforcement mechanism at all — both live tools (Claude Code, Codex) do
have a working `PreToolUse`-hook-equivalent mechanism (confirmed live for
both, via #142/#150/#151/#152's shipped work). The gap is design/coverage,
not "a tool exists with no lever to pull."

## Next steps (not this issue's scope)

If this design is accepted, file separate, sized implementation issues
for: (a) the Lane 1 `PreToolUse` guard + `APPLICATION_SOURCE_ROOTS`
config + `LANE1_TOOLING_EXCEPTION` launcher flag, and (b) the Lane 2
data-mutating-script flag-pattern guard for `gate_codex_tool.py` (and
`block_lane1_status_claims.py`, if Claude Code ever fills Lane 2). Per
harmonic-forge#115's own scope, no such issues are filed as part of this
spike — that's explicitly the operator's call after reviewing this
document.
