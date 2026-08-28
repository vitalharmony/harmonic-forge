# ADR-007: multi-agent adapter contract, capability tiers, and directive ownership

**Date:** 2026-08-21
**Status:** Accepted
**Decider:** Marc Mangus (platform owner)
**Resolves:** harmonic-forge#320 — the five decisions harmonic-forge#317's
source doc left open, plus the adapter contract itself
**Amends:** `3-lane-protocol.md` (new "Multi-agent adapter contract"
section, cross-referenced from § Per-CLI launch wiring)
**Governs:** harmonic-forge#322 (launcher), #323 (model-tier mapping),
#324 (surface synchronizer), #325 (parity suite), #326/#327 (Lane 3 tiers),
#362 (Lane 1/2 admin policy)

## Context

The three lanes are defined by role, not by vendor — that has been the
stated architecture since `harmonic-forge.md` v0.1. In practice the
platform grew Claude-shaped: every enforcement mechanism is a Claude Code
`settings.json` hook, every directive file is `CLAUDE.md`, and `LANE_CLI`
was an escape hatch rather than an interface. harmonic-forge#317 set out to
make Claude, Codex and Gemini genuinely interchangeable, and its survey
found that "supported" had never been defined: there was no statement of
what an agent must provide to fill a lane, and therefore no way to say
whether a given agent could.

Two findings from that survey drive most of what follows.

**The enforcement point is the launcher, not the repo.** Gemini's
workspace policy tier (`.gemini/policies/` checked into a repo) is
non-functional in 0.56.0 (upstream google-gemini/gemini-cli#18186). The
working mechanism is `--admin-policy <file>` passed at launch, verified
live on harmonic-forge#326: an admin-tier `deny` survives `--yolo`, and the
denied tools are removed from the model's tool list entirely rather than
refused at call time.

**Directive prose is never enforcement.** A rule written in `CLAUDE.md`
tells an agent what not to do; a hook or a policy prevents it. The platform
has been conflating the two, because with a single agent whose hooks were
always loaded the distinction never had to be drawn. It does now: the same
directive corpus produces a hard boundary under Claude Code and produces
nothing at all under an agent that never loads the hooks.

## Decision

### 1. "First-class" means Lane 1 and Lane 2 supported; Lane 3 is per-capability and experimental until qualified

An agent is first-class when it can fill Lane 1 and Lane 2 under the
contract in § 6. Lane 3 is not included, and is not a single yes/no: it is
a set of capability tiers, each individually qualified with its own
adversarial suite (harmonic-forge#326 for tier 1, #327 for tiers 2–4).

**Alternative rejected — all-three-lanes-or-nothing.** Cleaner to state,
and wrong in both directions. It would have blocked Gemini from Lane 1,
where it is useful today and where nothing it does depends on write
enforcement; and it would have implied that qualifying Lane 3 is one
decision rather than four independent security boundaries. The tiers are
where the actual risk lives, and collapsing them into a per-agent flag is
what produces a "Gemini is approved" claim that means less than it sounds.

### 2. `docs/agent-foundation.md` is the neutral foundation; the three entrypoints are thin real files

Each repo carries one vendor-neutral `docs/agent-foundation.md` plus
`CLAUDE.md`, `AGENTS.md` and `GEMINI.md` as thin tool-native entrypoints
that load it. **None of the three is a symlink to another.**

**Alternative rejected — symlink `GEMINI.md` → `CLAUDE.md`.** It is one
command, it is what HRSE2 does today, and it is wrong: a Gemini session
following that symlink loads Claude Code's identity, its hook mechanics,
its permission-mode vocabulary and its skill system, none of which exist
for it. The failure is not a missing feature — it is an agent confidently
operating from another tool's mechanics. Real files also let tool-specific
notes diverge (Gemini's needs the OAuth launch caveat; Codex's needs the
no-build-step note) without duplicating the foundation. **Alternative rejected — keep `CLAUDE.md` as the canonical document and have
the other two load *it*.** No new file, and it matches where the content
already lives. Rejected because it makes one vendor's entrypoint the
foundation every other vendor inherits: the shared content would keep
accreting Claude-specific mechanics by proximity, and the neutral/tool-native
split this decision exists to create would erode back into the state it was
meant to fix. A neutral file nobody's tool loads directly is what keeps the
boundary honest.

Implemented for
`harmonic-forge` and `cymagraph-infra` in harmonic-forge#321; HRSE2's
symlink is tracked as vitalharmony/hrse#1178 and is a known defect, not the
pattern.

### 3. `laneN --agent <tool>` is the canonical interface; `LANE_CLI` is compatibility only, and the two are mutually exclusive

A closed registry: an unrecognized `--agent` value is a hard error, never an
exec attempt. `LANE_AGENT` is exported alongside `LANE`, both immutable for
the session's lifetime. `LANE_CLI` is retained for aliases such as
`claude-api`. **Passing both is an error, not a precedence question.**

**Alternative rejected — define a precedence (`--agent` wins).** A
precedence rule is silent by nature: the operator who set `LANE_CLI` in a
shell profile and then typed `--agent gemini` gets a session that ignores
half of what they said and reports nothing. Refusing costs one error
message and removes a class of confusion entirely.

**Alternative rejected — keep `LANE_CLI` as the only interface.** It is an
arbitrary string execed directly, which is not a registry and cannot carry
per-agent defaults, version floors, or safety profiles. The interim
`tools/lane/_cli_launch.sh` (harmonic-forge#318) is a `case` over that
string and is explicitly a stopgap this ADR expects #322 to replace.

### 4. Lane 3 protection covers repository files, Git, GitHub, services, network, and live data

Not merely source-file edits. A boundary that stops `write_file` and allows
`run_shell_command` is not a boundary; neither is one that stops both and
allows `gh issue close`.

**Alternative rejected — file-write protection as the definition.** It is
the intuitive reading of "Lane 3 never fixes anything" and it is the one
that produced the current gap: `batch_auth.py` governs `gh issue close` /
`gh pr merge` precisely because *editing nothing* was never sufficient.

### 5. Narrow GitHub, test, and migration wrappers are required before the corresponding capability is enabled

A capability is enabled by a wrapper that constrains the invocation, never
by allowing the underlying tool. The wrapper's threat model must state what
remains possible through it — `pytest` executes arbitrary code by design
via `conftest.py`, and a wrapper that hides this rather than naming it is
worse than no wrapper.

**Alternative rejected — allowlist the tool, rely on the agent's judgment
plus directives.** This is decision 4's failure again in a different place,
and it is the exact thing "directive prose is never enforcement" denies.

## 6. The adapter contract

An agent filling lane *N* must provide all of the following.

| # | Requirement | Why it is load-bearing |
|---|---|---|
| 1 | **Immutable `LANE` and `LANE_AGENT`** in the process and its whole subprocess tree | Every hook and every self-check keys on `LANE`; a mutable value means a session can talk itself into another lane's authority |
| 2 | **Correct worktree**, entered by the launcher, never by the agent | Lane isolation is directory isolation |
| 3 | **Scoped GitHub credentials** (`GH_CONFIG_DIR` per account) | Cross-account writes are silent and attributable to the wrong identity |
| 4 | **Canonical context** — loads the repo's `docs/agent-foundation.md` through its own native entrypoint | An agent operating from another tool's mechanics is worse than one operating from none |
| 5 | **Lane-appropriate permissions**, applied at launch and un-removable via passthrough args | See § 9 |
| 6 | **Guard equivalence** for each mechanism the reference implementation enforces | Enumerated in § 7 |
| 7 | **Model-tier mapping** — a `deep` issue routes to the agent's high-tier model | harmonic-forge#323; absent mapping must fail loudly, not fall through |
| 8 | **Versioned parity suite** — re-runnable on CLI upgrade, recording the qualified version range | harmonic-forge#325; a qualification is against a version, not against a tool |

**A gap in items 1–5, 7 or 8 disqualifies the agent for that lane.** Item 6
is the exception, and the exception is deliberate: guard equivalence is a
*set* of individually-named mechanisms, and requiring all of them before any
lane is usable would decommission an agent in production today over a gap
nobody has been bitten by. So an item-6 gap is either **named and accepted**
in § 8's residual-gap list, or it is disqualifying. It is never silent.

## 7. Guard equivalence — the concrete matrix

Claude Code's enforcement is a set of `settings.json` `PreToolUse` hooks in
`harmonic-forge/tools/hooks/` (plus `tools/gh/block_closing_keywords.py`).
**Codex has its own hook mechanism** — `.codex/hooks.json` in the project,
with script hashes trusted in `~/.codex/config.toml` — and a live guard,
`HRSE2/scripts/gate_codex_tool.py`, which is `LANE`-conditional
(harmonic-forge#152). Gemini has no hook mechanism at all; its only
enforcement is an admin-tier policy passed at launch.

| Guard | Protects | Lanes | Codex | Gemini |
|---|---|---|---|---|
| `batch_auth.py` / `batch_gate.py` | `gh issue close`, `gh pr merge` | all | **gap — accepted, see § 8** | admin-policy deny (#362 for L1/2, #326 for L3) |
| `block_lane1_status_claims.py` | raw GitHub issue-post transports; auto-close keywords in a PR body; Lane 2 writing into the main checkout; `gate-*` tasks restricted to `LANE=3` | all | partial — `gate_codex_tool.py` covers raw issue posting for Lane 1 (harmonic-forge#190); the main-checkout and `gate-*` restrictions are **gaps** | admin-policy deny (#362 for L1/2, #326 for L3) |
| `block_lane2_status_claims.py` | raw GitHub issue-post transports for `LANE=2` (harmonic-forge#371 — Lane 2's own unverified status claims, previously carved out of `block_lane1_status_claims.py` entirely) | 2 | **supported** — `harmonic-forge/.codex/hooks.json`, wired directly to the canonical module (no per-repo wrapper needed, unlike HRSE2's `gate_codex_tool.py`) | **gap** — no admin-policy equivalent authored yet; not in this issue's scope |
| `tools/gh/block_closing_keywords.py` | closing keywords in PR bodies, `gh issue comment`/`edit`, and `gh api -X PATCH` on comments | all | **gap** | admin-policy deny (#362) |
| `block_irreversible_ops.py` | unrecoverable git/filesystem ops (asks; does not deny) | all | partial — `gate_codex_tool.py` denies Lane 3 git mutations, `sudo`, `sh -c`/`python -c` indirection, package installs | admin-policy deny (#362 for L1/2, #326 for L3) |
| `model_tier_gate.py` | `deep` issue on a low-tier model | all | **supported** — `CODEX_HIGH = "gpt-5.6-sol"`, wired via the `^apply_patch$` matcher; HRSE2 confirmed live 2026-08-09 | **gap — #323** |
| `deny_lane3_ae_self_post.py` | Lane 3 self-authorizing | 3 | **gap** | admin-policy deny (#326) |
| `deny_advisory_subagent_gh_writes.py` | advisory subagents writing to GitHub | 1 | **gap** | admin-policy deny (#362) |
| `lane3_cloud_cli_policy.py` | `kubectl`/`doctl` write ops inside a gate | 3 | **gap** | admin-policy deny (#326) |
| `block_data_migration_close.py` | closing a data-migration issue before it is executed | all (no `LANE` check) | **gap** | admin-policy deny (#362) |
| `block_stale_script_execution.py` | running a script from a checkout missing its fix | all | **gap** | not yet designed |
| `block_inline_prose.py` | multi-line prose through a bash string literal | all | **gap** | not yet designed |
| `mypy_cwd_trap.py` | wrong-cwd `mypy` invocation | all | **gap** | not yet designed |
| `remind_gate_readiness_sweep.py` (PostToolUse) | gate-readiness sweep reminder | 1 | **gap** | not yet designed |

**`batch_auth.py`'s gap is the sharpest and deserves naming.** Its design
deliberately removed the static `permissions.ask` rule for those two command
classes so that `decide()` could be the sole full-time decision, failing
*closed* on anything it cannot resolve. That is correct for Claude Code and
**inverts for an agent that never runs the hook**: with no hook deciding and
no static rule remaining, nothing prompts at all. Verified 2026-08-21 — no
`gh issue close` / `gh pr merge` entry exists in `ask`, `deny` or `allow` in
either `~/.claude/settings.json` or `harmonic-forge/.claude/settings.json`.
An enforcement design that is safe-by-default under one agent is fail-open
under another. That is the practical content of "directive prose is never
enforcement," and the reason #362 exists.

**#362 shipped, and its "admin-policy deny" cells above are accurate for
Lane 1 only.** `gemini-lane1.toml`'s narrow, default-deny-plus-allowlist
shell posture genuinely enforces most of the rows above (raw GitHub
posting, closing keywords, irreversible git ops, all fall outside its
allowed `commandPrefix`/`commandRegex` set and hit the catch-all deny).
`gemini-lane2.toml` deliberately leaves `run_shell_command` fully open --
confirmed live (harmonic-forge#362's own pitch-inspection) that an
args-scoped deny on an otherwise-open shell is bypassable via one level of
wrapper indirection, the identical gap `block_irreversible_ops.py`'s own
docstring concedes for Claude. So for Lane 2, every row above whose only
Gemini mechanism is "admin-policy deny (#362)" is **unenforced** -- Lane 2
under Gemini has no equivalent of `batch_auth.py`, `block_closing_keywords.py`,
`block_irreversible_ops.py`, or `block_data_migration_close.py` today. Only
the four whole-tool global denies (`write_file`/`replace` for Lane 1 only,
`activate_skill`/`invoke_agent` for both lanes) are real, structural
boundaries -- proven live, not merely schema-valid, by
`tools/lane/policies/canary/run_canary.py` (16/16 passing at time of
writing). See that file's own header and `gemini-lane2.toml`'s comments for
the full reasoning; not repeated here to avoid a second copy drifting.

## 8. The acceptance matrix

Concretely enough for harmonic-forge#325 to implement as tests. Status as of
this ADR:

| | Lane 1 | Lane 2 | L3 tier 1 (static review) | L3 tier 2 (tests) | L3 tier 3 (live/browser) | L3 tier 4 (migrations) |
|---|---|---|---|---|---|---|
| **Claude Code** 2.1.239 | supported | supported | supported | supported | supported | operator-launched |
| **Codex** 0.149.0 | supported¹ | supported¹ | unqualified | unqualified | unqualified | operator-launched |
| **Gemini** 0.56.0 | **supported** (#318, now admin-policy-protected, #362) | **partially supported²** (#362) | **blocked** — #326 | **blocked** — #327 | **blocked** — #327 | **blocked** — #327 |

¹ with accepted residual gaps, below.
² whole-tool global denies (`activate_skill`/`invoke_agent`) are real,
canary-proven boundaries; `run_shell_command` is intentionally
unmediated (§ 7's #362 note above) -- AC4 (`gh issue close`/`gh pr merge`
demonstrably unavailable) is explicitly NOT met. Neither lane's policy is
yet proven un-removable via a passthrough `--admin-policy` -- blocked on
harmonic-forge#322, same dependency F326 already carries for Lane 3.

### Vocabulary — every cell is exactly one of these

| Value | Means | Testable predicate for #325 |
|---|---|---|
| **supported** | Contract items 1–5, 7, 8 met; every item-6 gap either closed or listed as an accepted residual gap | Parity suite passes for that cell at the recorded CLI version |
| **unqualified** | No suite has been run. Not a claim that it is unsafe or broken — a claim that no evidence exists | No suite exists for the cell; asserting "supported" without one is the failure the suite tests for |
| **blocked — #N** | A named, open issue must close first | Issue #N is open |
| **operator-launched** | Not a mode any lane session may enter on its own; requires the five bindings in #327's tier 4 | A profile missing any of the five bindings is refused |

### Accepted residual gaps

Named here so they are falsifiable rather than invisible. Each is a
deliberate acceptance, not an oversight, and each is disqualifying the moment
it is no longer accepted.

- **Codex, Lanes 1–3: no `batch_auth`/`batch_gate` counterpart.** A Codex
  session can run `gh issue close` / `gh pr merge` with no authorization
  check. Accepted because Codex has been in production use across both lanes
  since well before this contract existed and this gap has not produced an
  incident; the alternative is decommissioning a working agent by ADR. It
  should be closed the same way #362 closes Gemini's — via `.codex/hooks.json`
  and `gate_codex_tool.py`, which already exist and already deny other command
  classes.
- **Codex, Lanes 1–3: the remaining `gap` rows in § 7.** Same reasoning, lower
  individual severity.
- **Codex: `codex exec --sandbox read-only` does not reliably block a file
  write** (found live, harmonic-forge#366). The model's file-edit tool can
  complete a write even when a shell-redirection attempt to the same
  location is correctly denied at the OS level. Accepted because
  `cross_family_call.sh`'s `read-only` posture is new surface, not an
  existing dependency, and Gemini's equivalent boundary (this ADR's § 10)
  closed on live evidence in the same issue — Codex's should follow the
  same admin-policy-shaped pattern rather than being designed under this
  amendment's narrower scope. Flagged for a follow-up, not silently carried.

**Reading rules, all three of which are the point of having a matrix:**

- **Tiers are per-capability, not per-agent.** Qualified for tier 1 confers
  nothing about tier 2. Each cell is earned by its own suite.
- **A cell is qualified against a CLI version**, recorded in the parity suite.
  A CLI upgrade invalidates the cell until the suite is re-run.
- **"Unqualified" must never be shortened to "supported" in prose.**

## 9. The enforcement point is the launcher, not the repo

**An agent started outside `laneN` carries no lane enforcement whatsoever.**
This is already true of Codex's `--sandbox read-only` and of Gemini's
`--admin-policy`; this ADR makes it a named, deliberate property rather than
an emergent accident of where flags happen to be set.

Two consequences follow, and both are load-bearing:

1. A repo-tracked policy file is **reviewable, not active.** It becomes a
   boundary only when a launcher passes it. Checking one in and assuming it
   applies is exactly the mistake Gemini's non-functional workspace tier
   invites.
2. **The launchers themselves become the highest-consequence code in the
   platform.** Every session in every repo starts through them, and a
   regression is silent and total. This is why harmonic-forge#322 is 3-lane
   rather than Tooling Exception, and the reasoning belongs here rather than
   only in that issue.

## 10. Cross-family headless calls (harmonic-forge#366)

Any of Claude, Codex, or Gemini may invoke one or two sibling CLIs headlessly
from inside a skill or subagent, receive a structured payload, and continue.
`tools/lane/cross_family_call.sh` is the helper; each family's own headless
form (`claude -p`, `codex exec`, `gemini -p`) is one Bash call away, but the
per-CLI flags, the read-only boundary, and the cold-brief contract are
centralized here rather than repeated at every call site.

**Motivation.** Every adversarial role in the protocol —
`preclose-inspection`, `pitch-inspection`, `sticky-wicket` — is a subagent of
the session that invokes it: same model family, same priors, same blind
spots. The 2026-08-22 tooling audit measured this directly, run as one
prompt in three clean windows: Codex refuted four findings Claude had
discarded, Claude refuted three Codex discards, and none of that seven was
visible from inside one family. It fell out of a 2-of-3 vote.

**Family order is fixed by the caller — Gemini is never second.**

| caller | 2nd | 3rd |
|---|---|---|
| Claude | Codex | Gemini |
| Codex | Claude | Gemini |
| Gemini | Claude | Codex |

A two-family call (`--families 2`) uses the caller plus its 2nd column;
`--families 3` adds the 3rd. This is the operator's decision (2026-08-22),
fixed in the helper so no skill ever picks a target by availability or mood.

**Cold context is mandatory, not incidental.** Every cross-family call ships
a self-contained brief to a file — acceptance criteria, diff, execution
boundary — and passes only that. No memories, no loaded rules, no
conversation. This is what makes a headless call *better* than a
same-session subagent for adversarial work: the refuter cannot anchor on the
reasoning that produced the defect.

**Postures, and where the boundary actually lives.**

- `probe` is the only posture that ever passes Gemini `--yolo`. It requires
  an isolated scratch directory (`--cwd`), created and removed by the
  caller, never a shared lane worktree. This is the posture
  `preclose-check`'s known-answer probe uses to prove a family can find a
  seeded defect at all before it gets a seat on a vote.
- `read-only` is the posture the real consumers (`preclose-check`,
  `sticky-wicket`) use for the adversarial call itself. **Its Gemini
  boundary is an admin-tier deny policy
  (`tools/lane/gemini-read-only-deny.toml`, loaded via `--admin-policy`),
  not `--approval-mode plan`.** The original design used plan mode; live
  reproduction on this issue showed the model can still call `write_file`
  under `--approval-mode plan` and narrate a false success while doing so.
  `--admin-policy` is the mechanism harmonic-forge#326 proved live: an
  admin-tier `deny` survives `--yolo`, and the denied tools are removed from
  the model's own tool list entirely, rather than merely refused at call
  time. That removal — not the model's self-reported summary — is what the
  helper's live verification checks (file-existence on disk, not the
  `report.summary` text).
- Codex's `read-only` posture uses `codex exec --sandbox read-only`, and
  `probe` uses `--sandbox workspace-write` plus `-C <cwd>`. **Live testing
  during this issue found `--sandbox read-only` does not reliably block a
  file write** — the model can complete the write via its file-edit tool
  even though a shell-redirection attempt is correctly blocked at the OS
  level. This is a real, reproduced gap, not a suspicion; it is carried
  forward as an accepted residual gap in § 8 below, the same pattern already
  applied to Codex elsewhere in this ADR, pending a repo-side deny mechanism
  for Codex equivalent to Gemini's admin policy.

**Normalized stdout envelope.** One JSON-lines record per invoked family:

```
{"family":"codex","posture":"read-only","status":"ok|process-error|invalid-report","exit_code":0,"report":{"summary":"…","findings":[{"claim":"…","evidence":"…"}]},"native":{…}}
```

`status: invalid-report` when the native output cannot be parsed into the
`report` shape — the helper never invents findings. `native` preserves each
CLI's own JSON for diagnosis.

**Which roles use two families, which use three.**

| Consumer | Families | Why |
|---|---|---|
| `preclose-check` / `preclose-inspection` (hrse#1208) | 3 | Maps the existing panel-size rule onto families: the 2-of-3 vote is what makes a refutation actionable. First consumer. |
| `sticky-wicket` | 3 | Rare, high-stakes; three families disagreeing is itself the answer. |
| `pitch-inspection` | 2 | One pass, one verdict, escalation already goes to the operator. |
| sprint-plan summary (hrse#1210) | 2, opt-in | `inferred` dependency edges confirmed by two families are more trustworthy; behind a flag, rate-limit constrained. |
| tooling-audit | 3, by construction | Not yet a skill; the 2026-08-22 prompt is the spec for one. |
| `product-strategy` | 1 (optional 2nd) | Synthesis; a second-family red-team on a build-vs-adopt call is a flag, not a default. |
| `lane1-gate`, `lane3-gate`, `verification-gate` | 0 | Mechanical, or they *are* a role. |
| `ai-review-queue-synthesis`, `meeting-debrief`, claim report | 0 | Synthesis. |

**Authority does not move.** A refuter spawned from another family is still
a finding the calling session interprets. It raises the floor; it is not a
Lane 3 gate, it never emits `PASS`, and closure authority stays with the
operator's explicit `Close H<N>`/`Close F<N>`.

## Consequences

**Gemini is approved for Lane 1 as of harmonic-forge#318, and for nothing
else.** That is a real, useful capability and it is also the whole of it.
Until #362 closes, a Gemini Lane 2 session has no write enforcement, and the
`batch_auth` inversion above means it can close issues and merge PRs with no
check — so the limit is not a formality to be waived when convenient.

**Codex's Lane 3 cells are unqualified, which is a new statement, not a new
condition.** Codex has been used in lanes for months; what changed is that
this ADR requires a suite before any Lane 3 cell may be *called* supported.
harmonic-forge#325 is scoped to discover those gaps rather than assume them.

**Every new Claude Code hook widens § 7 unless its counterparts are named
in the same change.** This ADR *recommends* that obligation and cannot
enforce it — an ADR is prose, and § "directive prose is never enforcement"
applies to this document as much as to any other. The mechanical version
belongs to harmonic-forge#325's parity suite, which can fail on a hook with
no matrix row. Filing that as an explicit AC on #325 is the follow-up; until
then § 7 is a checklist maintained by hand, and it started three rows short
in review — which is the argument for mechanizing it.

**`tools/lane/_cli_launch.sh` is explicitly interim.** harmonic-forge#318
shipped a `case` over `LANE_CLI` because it was the smallest thing that made
Gemini launchable. Precisely: the launchers satisfy item 2, and item 3 via
`tools/lane/_gh_config_dir.sh`; item 4 holds per-repo (true for
harmonic-forge and cymagraph-infra after #321, **false for HRSE2** while its
`GEMINI.md` is a symlink); **item 1 is only half met — `LANE` is exported,
`LANE_AGENT` does not exist anywhere in the repo yet** and is #322's own AC2.
Items 5–8 are unmet.
harmonic-forge#322 replaces it with the registry this ADR specifies; that is
the plan, not scope creep.

## Deferred, deliberately

- **Where procedures live** (harmonic-forge#170's rules-vs-skills split) is
  named as a dependency by #320's issue body but is not decided here. This
  ADR governs *what the contract requires*; #170 governs *which file a given
  procedure is written in*. Writing this ADR ahead of #170 was an operator
  decision, and the two do not collide: nothing above depends on where a
  procedure currently sits.
- **Per-agent skill/command surfaces** (`.claude/skills/` vs
  `.gemini/commands/`) are harmonic-forge#324's synchronizer problem.
  Contract item 4 requires canonical context, not a particular mechanism for
  distributing it.
