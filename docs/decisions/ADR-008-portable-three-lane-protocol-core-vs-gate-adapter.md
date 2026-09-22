# ADR-008: protocol core vs. per-repo gate adapter, the skill-ownership test, and the distribution mechanism

**Date:** 2026-09-22
**Status:** Accepted
**Decider:** Marc Mangus (platform owner)
**Resolves:** harmonic-forge#703 — P1 of harmonic-forge#208's "portable
3-lane protocol" epic. Lands first; P4, P5, P6 each need a ruling made here.
**Governs:** harmonic-forge#208 (epic), and every future child that migrates
a skill, hook, or script from a consuming repo (HRSE2, cymagraph-infra,
openclaw-projects) into `harmonic-forge`.
**Moves nothing.** This ADR is a decision record only — no file is moved,
renamed, or deleted by #703. P5/P6 execute against it.

## Context

The working assumption going into #208 was: *"All the skills, hooks, etc.
we've defined up to this point are part of the 3-lane protocol, not tied to
any particular project/repo"* (operator, 2026-09-21). Measured against the
actual tree, that is true of most of the surface and false for at least
three artifacts that read HRSE2's live Neo4j graph and HRSE2's process/port
model directly. #208 was originally sequenced so this reconciliation
happened last, once P4/P5/P6 had already made their own per-artifact calls
— which meant making the same judgment six times, ad hoc, with no shared
test. This ADR makes the ruling once, up front, so every later child cites
it instead of re-deriving it.

The operator's stated design goal is **"all in forge"**, with the burden of
proof on every exception (ruling, 2026-09-21): *"I'm open to a 2 tier model
but it needs to be justified as to why it is needed. All in forge is the
design goal."* An exception is justified only by showing the behavior is
**not expressible as protocol plus a manifest value** — not merely that it
currently reads HRSE2 state. "It touches the graph" is not, by itself, a
justification: a declared query string is a manifest value; a declared
per-repo ontology is not.

## Decision 1 — the two-tier model

**The protocol is repo-agnostic; the gate's environment adapters are not,
and should not be.** Platform (`harmonic-forge`) owns the protocol's
procedure and transport — the lane roles, the HITL gate language, the
handoff/spec/report shapes, the enforcement hooks that read `LANE`, the
launcher. Each consuming repo owns a declared **gate adapter**: the small
set of calls whose actual behavior depends on that repo's own runtime
(its graph schema, its process/port topology, its data shapes) and cannot
be reduced to a value substituted into an otherwise-identical procedure.

Three artifacts were proposed as candidate exceptions to "all in forge."
Each is argued individually below against the not-expressible-as-manifest-
value test, not accepted as a group.

### Exception 1 — `gate_residue.py` (716 lines): **ACCEPTED** (adapter-owned)

Sweeps Lane 3 fixture residue from HRSE2's graph by elementId manifest.
Its `--report` mode needs no live database connection, but that does not
make it manifest-only: the manifest it reports on is already a manifest of
**HRSE2 graph elementIds**, and constructing that manifest — deciding what
counts as "Lane 3 fixture residue" in the first place — requires walking
HRSE2's own ontology (`Experience`, `Activity`, `Opportunity` node shapes,
which edges a fixture leaves behind, which labels are test-only vs. real).
That is a *procedure* specific to HRSE2's domain, not a value one could
declare in a manifest and hand to a repo-neutral traversal — the same
distinction Decision 3's skill-ownership test draws between "the procedure
is identical and only inputs differ" (platform) and "the procedure itself
is domain-specific" (project). The platform owns *when* this sweep runs
(the Lane 3 gate lifecycle hook) and *what shape its result must take*
(pass/fail plus an evidence artifact, per `rules/testing-gate.md`); HRSE2
owns the sweep's own graph-walking logic as its gate adapter.

### Exception 2 — `check_mergetarget.py` (69 lines): **REJECTED** (folds into platform + config)

A single Neo4j query against HRSE2 node shapes, checking the merge target
is what the issue claims it is. Unlike Exception 1, this is not a
multi-step domain procedure — it is one query plus one shape assertion.
Applying Decision 3's corollary directly: *"if a SKILL.md contains an
absolute path or a repo-specific command string, that string is config,
and the skill is platform-owned once extracted."* The same holds for a
Cypher string. The platform mechanism becomes "execute the declared
validation query against the declared connection, compare the result
against the declared expected shape" — three manifest values (query text,
connection env var name, expected-shape assertion), not a repo-specific
procedure. `check_mergetarget.py` does not clear the bar Exception 1
clears; it is the case the "it touches the graph" fallacy exists to catch,
and rejecting it is the demonstration that the test has teeth.

### Exception 3 — `check_lane3_marker.py`'s lease half / `lane3-gate`'s preflight half: **ACCEPTED** (adapter-owned)

Encodes HRSE2's port/process contention model: which ports the dev stack
binds, how to walk `/proc/*/cwd` to detect a live process already holding
a worktree, what "busy" means for `mise run gate-checkout`. This is
inspection of live OS process/socket state specific to how HRSE2 actually
runs its stack (podman-compose ports, `mise` task names, the specific
service set) — not a value substitutable into a generic procedure, because
the *check itself* (which ports, which process markers, which lockfile
convention) is what varies, and a different consuming repo with a
different service topology needs a genuinely different check, not the same
check with different arguments. Matches the disposition Decision 4 already
gives `lane3-gate` below: role-constraint half platform, scheduler-lease/
port-preflight half HRSE2 gate-adapter data.

**Summary: 2 of 3 proposed exceptions survive the test.** The rejected one
demonstrates the test is discriminating, not a rubber stamp for "currently
touches HRSE2."

## Decision 2 — publication posture

Operator ruling, 2026-09-21: *"it should be anonymized or not go."* The
transport scripts (`l1_post.py`, `post_lane_discussion.py`, and siblings)
carry several hundred `hrse#NNNN` citations narrating private-repo
incidents. Migrating that text into `harmonic-forge` (a public repo) means
either an anonymized account of the incident or no account at all. **A
bare cross-reference to a private issue number is itself a disclosure** —
naming `hrse#849` publicly discloses that something notable happened on
that private issue even without quoting its content — and is not a
substitute for anonymizing.

**The scrub happens during each migration, at the point the text is
already being read and moved** — never deferred to a later pass. A
deferred scrub is a second read of text already migrated, which is exactly
the kind of debt R-0352 requires a named trigger/owner/record for, and
which this platform's own history shows does not reliably happen later if
it can happen now instead.

**Also recorded: no platform script may default `--repo` to a consuming
repo.** Four sites currently default `--repo` to `vitalharmony/hrse`. A
platform script silently defaulting to one consumer's repo is itself a
two-tier violation in miniature — it treats one consuming repo as
privileged rather than requiring every caller to declare its target
explicitly, which is the same manifest-value discipline Decision 1 applies
to gate adapters. Each of the four sites gets `--repo` promoted to a
required argument (or read from the per-repo manifest introduced in
Decision 5) as part of whichever child issue touches it; none may ship
with an implicit consuming-repo default going forward.

## Decision 3 — the skill-ownership test, verbatim

> A skill is **platform-owned** when its *procedure* is identical in every
> repo and only its *inputs* differ. The inputs then live in a tracked
> per-repo config file (`.claude/<skill>.config.json`, validated against a
> platform-owned schema), never inside the skill. A skill is
> **project-owned** only when the procedure itself — not its arguments —
> is specific to that project's domain.
>
> Corollary: **if a SKILL.md contains an absolute path or a repo-specific
> command string, that string is config and the skill is platform-owned
> once it is extracted.** The absence of a config mechanism is never
> evidence that a skill is project-local.

This does not re-litigate #208's existing Non-goal (*"A verification
gate's actual commands are inherently per-project — the mechanism for
expressing and distributing one is what generalizes, not the commands"*).
It states the general rule that Non-goal was one instance of: the same
extraction move (repo-specific string → manifest value) that turns
"verification-gate's commands" from project-local into platform-mechanism
is the move Decision 1 above applies to `check_mergetarget.py`, and the
move that fails for `gate_residue.py`'s ontology-walking and the lease
check's process-inspection, because neither reduces to a string.

## Decision 4 — the disposition table

Complete for every skill in `HRSE2/.claude/skills/` and
`harmonic-forge/skills/` as of 2026-09-22 (verified live against both
directories, not recalled) — a skill absent from this table is a gap in
this ADR, not an implicit "project" ruling.

| Skill | Repo(s) present in | Disposition |
|---|---|---|
| `preclose-check` | HRSE2 | Platform. Procedure repo-neutral; only `--repo` is input. |
| `lane1-gate` | HRSE2 | Platform + per-repo config (its steps are mise task names). |
| `verification-gate` | HRSE2 | Platform mechanism + per-repo command config. Five hardcoded absolute-path commands are config, not procedure; `mypy_cwd_trap.py` is already a platform hook, proving the structure is platform-ownable. |
| `sprint-plan` | HRSE2, harmonic-forge | Platform mechanism (already done, F104) + per-repo config (unfinished). |
| `lane3-gate` | HRSE2 | **Split**, per Exception 3 above. Role-constraint half platform; scheduler-lease/port-preflight/`check_port_owner.py` half is HRSE2 gate-adapter data. |
| `meeting-debrief` | HRSE2 | Project. Domain procedure (graph write, follow-up email composition) is HRSE2-specific end to end. |
| `tailored-resume` | HRSE2 | Project. Domain procedure (EvidenceClaim/PersonaProfile selection) is HRSE2-specific end to end. |
| `ai-review-queue-synthesis` | HRSE2 | Project. Domain procedure (Drive review-queue synthesis against HRSE2/harmonic-forge backlog) is specific to this operator's workflow, not a repo-neutral mechanism. |
| `belt-and-suspenders` | HRSE2, harmonic-forge | Platform. Already parameterized purely by `LANE`; ships identically to both directories today (post-#540 fix) — no repo-specific procedure or string found. |
| `impl-worktree` | harmonic-forge | Platform. Procedure (`/tmp/<repo>-<issue>-impl` creation/provision/cleanup) is repo-neutral by construction — `<repo>` and `<issue>` are its only inputs. |
| `memory-triage` | harmonic-forge | Platform. Operates on the shared memory store's rule registries, not on any project's domain data. |
| `protocol-failure` | harmonic-forge | Platform. Captures protocol failures generically; explicitly excludes product/code bugs by its own description. |
| `_stub` | harmonic-forge | **Neither** — declared non-functional by its own frontmatter (harmonic-forge#169's proof-of-mechanism artifact, never distributed by default). Excluded from the platform/project split; recorded here so its absence above is not mistaken for an oversight. |

## Decision 5 — distribution mechanism

Operator selected **versioned copy + drift check** over symlinks
(2026-09-21). Recorded here because the reasons are measured, not
aesthetic, and will otherwise be re-argued the next time someone proposes
symlinks for convenience:

- **Gitignored symlinks cannot travel with a clone**
  (`forge_onboard.py:195`) — a fresh checkout of a consuming repo gets
  nothing until `forge-onboard` is re-run by hand.
- **They resolve at runtime with no pinning or integrity check** — anything
  able to write `~/harmonic-forge` changes behavior in every onboarded repo
  at once. This has already happened twice, benignly: 32 symlinks across
  four repos were repointed at `/tmp/forge-498-impl` (`:59`), and 8 of
  forge's own symlinks were left dangling (`:132`).
- **Live-measured drift, 2026-09-21:** `HRSE2-lane2` was 46 commits behind
  `origin/main`, running `l1_post.py` at 79,631 bytes against main's
  89,685 and `post_lane_discussion.py` at 16,562 against 18,952.
  `HRSE2-lane3` was current at the same moment, so the drift was silent
  *and* asymmetric between two worktrees of the same repo — nothing
  detected it.

`tools/lane/check_installed_lane_sources.py` (harmonic-forge#645) already
implements drift detection for launchers on this exact model (byte/hash
comparison against the canonical source). **This epic generalizes that
check rather than adding a fifth distribution mechanism** — the same
comparison, extended to cover skills, hooks, and rule files, not a new
tool alongside it.

## AC4 — the gate-adapter interface

Specified concretely enough for P5 to cut the seam without a second design
pass.

**Declaration point.** Each consuming repo carries a tracked manifest,
`.claude/gate-adapter.json`, validated against a platform-owned JSON
Schema shipped at `harmonic-forge/schemas/gate-adapter.schema.json`. Absent
manifest = no adapter registered = platform gate steps that require one are
skipped with an explicit `BLOCKED` finding — same fast-fail posture as a
genuine external precondition gap (`rules/testing-gate.md` rule 8) — never
silently no-op'd.

**What the manifest declares**, one key per adapter-owned capability
identified in Decision 1/4 above:

```json
{
  "residue_sweep": { "module": "scripts/gate_residue.py", "entrypoint": "sweep" },
  "merge_target_check": {
    "query": "MATCH (n:Experience) WHERE elementId(n) = $id RETURN count(n) AS c",
    "connection_env": "NEO4J_URI",
    "expected_shape": { "field": "c", "op": "gt", "value": 0 }
  },
  "lease": { "module": "scripts/check_lane3_marker.py", "acquire": "acquire_lease", "release": "release_lease", "check_owner": "check_owner" }
}
```

`merge_target_check` carries no repo-specific code path at all — per
Exception 2's rejection, it is fully expressed as manifest values consumed
by a platform-owned executor (`tools/gate/run_merge_target_check.py`,
new in P5) that runs the declared query, applies the declared shape
assertion, and reports pass/fail in the standard gate-finding format.

`residue_sweep` and `lease` declare a **module path + named entrypoints**,
because their procedures are adapter-owned per Exceptions 1/3: the
platform calls `import_module(manifest["residue_sweep"]["module"])` then
`getattr(module, entrypoint)(**standard_kwargs)`, where `standard_kwargs`
is a fixed, platform-defined dict (issue id, target SHA, report-only flag)
— the adapter module owns everything downstream of that call, the platform
owns everything upstream of it (when it's called, what shape the return
value must have: `{"status": "pass"|"fail"|"blocked", "evidence": [...]}`).

**What the platform calls, and where.** Three fixed call sites gain a
manifest-driven adapter hook, each optional per the manifest above. Two
belong to `lane3-gate`'s own lifecycle; the third belongs to Lane 1, per
the ownership `rules/testing-gate.md` rule 3 already assigns — this ADR
does not relocate that ownership, only adds the adapter call inside it:

1. **Lane 1's own gate-readiness sweep**, run before Lane 3's first
   execution attempt and owned by Lane 1 exactly as `rules/testing-gate.md`
   rule 3 already states ("This is a Lane 1 responsibility, not Lane 3's")
   — `merge_target_check` if declared, folded into the sweep's own
   per-case readiness report.
2. Immediately after Lane 3's PASS/FAIL verdict, before the gate report is
   posted, inside `lane3-gate` itself — `residue_sweep` if declared, its
   result folded into the same report.
3. At `gate-checkout` time, before a branch switch in a shared lane
   worktree (existing `check_worktree_busy.py` call site, also inside
   `lane3-gate`) — `lease.acquire`/`check_owner` if declared, in place of
   (not alongside) the current HRSE2-specific `check_lane3_marker.py` call.

No adapter module executes with elevated privilege beyond what the calling
lane already holds — the platform does not grant a residue-sweep module
database access it wouldn't otherwise have; it is the consuming repo's own
script, imported in-process, same as today.

## Acceptance criteria — status

- **AC1.** Satisfied — decisions 1–5 recorded above; each of the three
  proposed exceptions argued individually and given an explicit verdict.
- **AC2.** Satisfied — the skill-ownership test and its corollary appear
  verbatim under Decision 3.
- **AC3.** Satisfied — the disposition table (Decision 4) covers every
  skill found live in both `HRSE2/.claude/skills/` and
  `harmonic-forge/skills/` as of this ADR's date, including the three not
  named in the issue body (`impl-worktree`, `memory-triage`,
  `protocol-failure`) and the non-functional `_stub`.
- **AC4.** Satisfied — the gate-adapter interface (manifest location,
  schema shape, call sites, calling convention) is specified above.
- **AC5.** Satisfied — this issue creates one new file and comments on
  harmonic-forge#208; nothing existing is moved, renamed, or deleted.
