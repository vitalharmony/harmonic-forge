# 3-Lane Protocol (Agent-Readable)

Condensed operational directives. For philosophy/rationale, see
`harmonic-forge.md`. This file is what agents load — no narrative prose.

```
[GitHub Issue] → Dev pulls ticket → [Local 3-Lane Loop] → GitHub PR
                                       │
                                 Lane 1: Blueprint — operator-assigned tool
                                 Lane 2: Muscle — operator-assigned tool
                                 Lane 3: Control Gate — operator-assigned tool
```

**Lanes are roles, not fixed tools.** All three lanes are filled by
whichever tool the operator running that session assigns. Devin Local /
Devin AA was this doc's original reference pairing for Lane 2/Lane 3 and
is retained below as a historical example where it clarifies intent —
Devin is retired from active use as of 2026-08 (see harmonic-forge#317's
multi-agent parity epic). Codex is a current live example for either
Lane 2 or Lane 3 per issue (see the operator's own project rules file,
e.g. `.windsurfrules`, for the exact trigger phrasing); Claude and Gemini
are the two tools #317 is qualifying for the same role, with Gemini's
qualification tracked capability-by-capability. Claude Code is Lane 1's
reference/default implementation — this platform's own authoring tool —
but is not fixed by name; an operator may assign a different tool to
Lane 1 via that tool's own equivalent mechanism (see § Lane 1 below).
Different collaborators (see Team Topology below) will likely bring their
own tool preferences for these roles — the protocol's actual requirements
(independent-eyes gating, no lane closes/merges on its own, HITL-gate
language, etc.) are tool-agnostic and apply identically regardless of
which specific tool is filling any lane on a given session.

## Lane 1 — Blueprint (reference tool: Claude Code)

*Claude Code is the reference implementation this section is written
against — an operator assigning a different tool to Lane 1 follows the
same requirements below, via that tool's own equivalent mechanism where
a Claude-Code-specific file/path is named. See `rules/universal-lane1.md`
(role-general Lane 1 directives) and `rules/universal-claude.md`
(Claude-Code-CLI mechanics) for the deeper directive set this section
summarizes.*

- Reads the GitHub issue (spec, acceptance criteria, labels).
- Maps required changes against the project's `CLAUDE.md`/`.windsurfrules`.
- Cites exact file(s) and line range(s) affected before writing the handoff.
- Produces a Lane 1 Handoff Artifact (`templates/lane1-handoff.md`).
- Includes one concrete test case per requirement in the handoff.
- **For test cases requiring interactive UI verification, front-loads
  concrete selectors/interaction paths already known from reading the
  code** (exact button labels, roles, click sequences) rather than telling
  Lane 3 to "verify X live" with no starting point. Real cost of not doing
  this: a handoff that just said "use Playwright for real verification"
  left Lane 3 to blind-guess selectors through repeated trial and error,
  costing significant time and command failures on #152 — compare to a
  handoff that already named exact button text, which went smoothly on
  #159. This doesn't replace Lane 3's own exploration where genuinely
  needed, it just removes the exploration Lane 1 could have shortcut by
  already having read the component.
- **Posts the handoff as a comment on the GitHub issue itself** — not just
  in chat. The issue is the permanent record: root cause, prompt, and (once
  Lane 2/3 finish) the diff and Lane 3 gate result all live on the same
  issue. This is deliberate — it's the raw material for any future
  cross-issue pattern analysis (recurring bug classes, handoff-quality vs.
  first-pass-success correlation), not just a courtesy copy.
- Never guesses at an ambiguous spec — stops and asks the Tech Lead.
- **A close that leaves work outstanding links a tracker in the same
  action.** If merging an issue's code leaves anything still to happen —
  a migration to run, a manual verification, an operator step, a
  follow-up someone agreed to — the close either does not happen yet, or
  it happens alongside a linked issue for the remainder. **A closing
  comment describing outstanding work is a record, not a tracker.**
  Nothing reads it, nothing surfaces it, and nothing degrades when it is
  ignored.

  Real cost (hrse#849, 2026-08-13): the code fix merged, the issue
  closed, and the closing comment itself said the run was *"still
  outstanding."* All 219 target rows were untouched. It blocked hrse#847
  and hrse#856 for days and was found only because a human happened to
  notice in conversation. The information was present, honest, and
  written down — and no part of the system was looking at it.

  This is stated as a rule and deliberately **not** enforced by tooling.
  A detector would have to decide from prose whether a comment describes
  outstanding work, and pattern-matching prose authored by the party
  being checked is a failure class this platform has now paid for
  repeatedly (hrse#859 took four review rounds to abandon it; hrse#871
  shipped with a silent 18% miss for the same reason). Where a specific,
  mechanically-detectable class of outstanding work exists, enforce
  *that* class instead of the general case — hrse#859 gates closing a
  `data-migration` issue on a label, and hrse#867/#871 sweep for the same
  invariant after the fact. Extend by adding classes, not by guessing at
  prose.
- Does not write production code directly (exception: explicit platform/
  tooling assignments — see `rules/universal-lane1.md`).

## Lane 2 — Muscle (qualified agents: see ADR-007 § 8)

*Devin Local was this section's original reference implementation — retired
as of 2026-08 (harmonic-forge#317). An operator assigning Claude, Codex, or
another tool to Lane 2 follows the same requirements below, with that
tool's own equivalent mechanism where a Devin-specific file/path is named.
See the project's own rules file (e.g. HRSE2's `.windsurfrules` "Codex
role" section) for how a given tool's Lane 2 assignment is actually
triggered and scoped.*

- Given a short trigger (e.g. "implement #N") rather than pasted content,
  **fetches the issue and Lane 1's handoff comment from GitHub directly**
  (`gh api repos/OWNER/REPO/issues/N/comments --paginate --jq '.[].body'`,
  REST — see harmonic-forge#220) before doing anything else — same
  expectation already placed on Lane 3 for its own independent issue read.
  Do not wait for or require the handoff to be pasted in chat.
- Executes exactly what the handoff specifies — no scope creep.
- Stays within the file targets the handoff defines, and this applies to
  tooling too — never invokes a task explicitly scoped to another lane
  (e.g. `gate-checkout`) even to verify its own change; check the
  underlying script instead.
- **3 fix attempts max on a single root cause; 4th failure stops and
  reports back instead of continuing to iterate.** This cap already
  existed for Lane 3 (see Escalation, below) but was never written for
  Lane 2 — a real gap: Lane 2 is the lane that does most of the iterative
  "try something, test it, try again" work on a genuinely hard bug, and
  had no explicit trigger to stop and ask for help instead of continuing
  to churn. Real incident: #152's focus-trap fix (a live-DOM behavioral
  bug, hard to iterate on blind without a visual feedback loop) took
  substantially more churn than it should have before Lane 2 landed a
  working fix. It got there, but an adversarial check-in after a few
  failed attempts — comparing notes with Lane 1 or escalating to the Tech
  Lead — would likely have converged faster than continuing to iterate
  solo. When reporting completion after multiple attempts, **include what
  was tried and ruled out**, not just the final working diff — that
  history is exactly what Lane 1's review should look at when deciding if
  the fix is sound or got there by luck.
- **Never executes the write/apply path of a data-modifying script —
  categorically, no exception, regardless of what the handoff says.**
  Lane 2 may write a migration/fix script and verify it via dry-run or
  fixture-only testing, but the actual execution against real data is
  never Lane 2's action — see Lane 3's execution authority below. This
  rule has no "unless explicitly authorized" clause on purpose: an earlier
  version of this rule had one, and it likely contributed to two real,
  confirmed violations in a row (#154 — Lane 2 hardcoded and ran a
  data-correcting script for a specific bad edge that was only ever cited
  as motivating context, not asked for as work; #155 — Lane 2 ran a
  properly-scoped migration's `--apply` flag against full production
  during its own implementation, when only dry-run/fixture verification
  was authorized). Both times, Lane 2 could correctly quote the
  then-current rule verbatim when asked directly — confirming this is not
  a stale-rules problem, it's that a textual prohibition alone doesn't
  reliably hold against the pull of "let me verify this actually works."
  The fix is structural, not another sentence: Lane 2 is never granted
  this capability at all, so there's nothing to correctly or incorrectly
  apply in the moment. Same posture on any other lane's own scope decision
  (e.g. Lane 3's `--execute` pass) — surface a fact, never frame or ask
  about it, and never assert readiness for the next lane.
- Reads the cited file(s) and quotes the root-cause line before editing.
- Obeys `rules/universal-agent.md` + language-specific rule files (300-line
  cap, no raw LLM/API calls outside the designated gateway, parameterized
  queries).
- **Posts to the issue on every stop, unprompted** (completion, BLOCKED,
  plan, correction, rebase — chat is not the record), via `l2_post.py`,
  which also closes the raw-transport hole above (harmonic-forge#371).

## Lane 3 — Control Gate (qualified agents: see ADR-007 § 8 — Codex's Lane 3 cells are *unqualified*, meaning no suite has been run, not that it is unsafe)

*Devin AA was this section's original reference implementation — retired
as of 2026-08 (harmonic-forge#317). An operator assigning Claude, Codex,
or another tool to Lane 3 must still meet every requirement below
(independent read before gating, no edits/fixes/commits, FAIL-and-report
rather than work around a blocker, no close/merge without explicit
operator instruction), via that tool's own equivalent mechanism where a
tool-specific file/path is named below. **Mechanical enforcement, not
prose alone, is what actually holds** — this project's own history (see
`.claude/skills/lane3-gate/SKILL.md`'s origin) is a hard-tool-restriction
gate built specifically because prose-only Lane 3 rules were violated
under pressure more than once. Any tool assigned to Lane 3 should get an
equivalent hard restriction (a permission/sandbox profile denying
write/edit/commit/push tools for that session) wherever the tool supports
one, not just an instruction file it's trusted to follow — see
harmonic-forge#317's capability-tier work for Gemini's version of this.*

- **Own skill file, per-project: `{project}/.claude/skills/lane3-gate/SKILL.md`**
  (repo-local — as of 2026-07-10, not yet synced across projects by
  `sync_rules.py`; currently HRSE2-only). This is a hard-wired enforcement
  mechanism for the never-fixes-anything constraint two sections below,
  originally drafted after diagnosing that prose rules alone weren't
  preventing rationalization past them ("I found the fix and it was easy"
  / "the fix is obviously correct"). The skill explicitly names
  `harmonic-forge/3-lane-protocol.md` and `rules/testing-gate.md` as
  authoritative — it is the enforcement layer, not a second source of
  truth, and should be updated to match if the two ever drift. Cites the
  real incidents (`#176`, `#52`) that produced it. If this pattern proves
  out, worth revisiting whether it should be added to `sync_rules.py`'s
  distribution so other projects get the same hard-wired protection, not
  just prose.
- Runs locally. Reads the GitHub issue independently — **never reads Lane
  2's code, or Lane 2's own implementation-report comment on the issue
  thread, before writing its test spec.** Lane 3 reads only: the issue
  body, Lane 1's original handoff comment, and (if present) a second Lane
  1 comment addressed specifically to Lane 3, written after Lane 2
  finishes, carrying any caveats the HITL gate turned up (real example,
  #153: "the named test-pair's data has since changed, construct a
  fixture instead"). Co-locating every lane's output on one issue is a
  deliberate convenience for Lane 1's own review — it must not become a
  backdoor letting Lane 3 anchor on Lane 2's self-report.
- Writes a test spec from the issue's acceptance criteria.
- Submits the test spec for Tech Lead HITL approval
  (`templates/hitl-test-review.md`) before executing anything.
- After approval, executes tests against Lane 2's implementation. See
  `rules/testing-gate.md` (standard) or `rules/frontend-ui-golden-path.md`
  (UI-only variant) for thresholds.
- **Interactive tool modes are never used for automated execution.** Any
  browser-automation invocation runs headless with a non-interactive
  reporter (e.g. `--reporter=line`) — never `--debug`, `--ui`, `codegen`,
  or any other mode that opens an inspector/GUI and blocks on human
  interaction. A non-interactive terminal can't surface that prompt, so
  the command hangs until a human notices and cancels it (real failure
  mode, #152). A command needing interactive confirmation is a signal to
  stop and ask, not to keep retrying variants hoping one works headlessly.
- **Lane 3 is the only lane authorized to execute a data-modifying
  script's write/apply path.** When an issue's scope is itself a data
  migration or correction (not just testing already-written application
  code — e.g. #155's shape), the test spec submitted for HITL approval
  must say so explicitly: the plan includes actually running the
  migration for real, not just verifying it against fixtures. HITL
  approval of that spec is the approval to execute it — reusing the
  pre-execution approval gate Lane 3 already has for every spec, closing
  the #154/#155 gap structurally: nothing runs against real data without
  HITL having approved that specific action first.
- **A data-migration issue does not close on the code merge — it closes
  when the migration has run, evidenced.** Real incident, hrse#849
  (2026-08-13): the code merged, the issue closed, and the closing
  comment itself said the run was "still outstanding" — all 219 target
  rows untouched, blocking two other issues for days until a human
  noticed in conversation. Merging the code satisfies every check that
  exists, so an unevidenced close looks legitimate. **Mark such an issue
  `data-migration` at filing time**; closing it then requires the
  `migration-executed` label, or `migration-abandoned` with reasoning in
  a comment when the run is deliberately not happening. Enforced by
  `tools/hooks/block_data_migration_close.py` (hrse#859).

  **The credential is a label, not text in a comment — an expensive
  lesson.** Four review rounds rejected comment-parsing designs; the
  decisive one (hrse#866) showed that once an example marker's exact
  format is published anywhere, *every published example is itself a
  valid credential* — hrse#866's own body carried a fenced example
  receipt naming hrse#849, and pasting that body onto the thread would
  have opened the gate for the exact issue the hook exists to protect.
  Narrowing where an example may legally appear (not blockquoted, not
  fenced, not indented) is a blocklist maintained against your own
  documentation, and it does not converge. A label is not this: applying
  one is a distinct action from naming one, and it carries an actor and
  timestamp in the timeline that pasted text does not. (Two related
  non-solutions, for the same reason: a gate-readiness sweep is a
  **pre**-execution artifact, and a Lane 3 gate report evidences that
  tests ran, not that rows changed — hrse#848 passed 12 of 13 checks with
  the thirteenth knowingly unexecuted.)

  **The load-bearing control is the after-the-fact sweep, hrse#867
  (shipped 2026-08-14)**: closed `data-migration` issues lacking
  `migration-executed`, folded into the standing hygiene pass — catches
  every close path the `PreToolUse` hook structurally can't see (GraphQL,
  heredoc bodies, the web UI). What it still doesn't close: nothing
  enforces the label is applied *at filing time* — hrse#849 was labelled
  64 minutes after it closed, so neither control would have caught it as
  it actually happened (hrse#871). The hook remains fail-open by design;
  it makes the mistake rare, not impossible.
- Blocked from committing until 100% of tests pass at the required coverage
  threshold.
- **Lane 3 never fixes application code, ever, under any circumstance —
  full stop** — self-introduced, pre-existing, trivial, or blocking test
  execution entirely, it makes no difference. Lane 3's only permitted
  write actions: writing/refactoring its own test specs and scripts, and
  executing an already-HITL-approved data-migration script's write/apply
  path (the one exception above — pre-approved, not an in-the-moment
  fix). Every other case: stop and report to Lane 1, or ask the human
  operator directly for live authorization (the correct pattern — human
  directs it live, Lane 3 never decides on its own). "3 auto-fix attempts
  max" below governs retrying Lane 3's own test-spec problems only (a bad
  fixture, a wrong assertion) — never modifying code under test. **Why
  this is structural, not just a boundary**: Marc: *"we WANT the tests to
  fail if L1/L2 built something wrong... defeats the purpose of the
  protocol"* if Lane 3 silently patches a failure instead. A test that
  gets patched by its own tester the moment it fails can never fail
  meaningfully again — a failing test is the protocol working, not a
  problem for Lane 3 to make go away. (Real incident underlying this
  rule, HRSE2 #176: one of three inline self-fixes during a single gate
  run was live human-authorized and fine; two were not — a real bug
  fixed instead of reported, and a plaintext credential hardcoded into a
  compose file instead of fixing the actual invocation problem.)
- 3 auto-fix attempts max on a single root cause (test-spec issues only, per
  above); 4th failure escalates to Tech Lead instead of retrying.
- After tests pass, performs a style/refactor pass per the project's
  `.windsurfrules` — **report-only, same absolute rule as above**: identifies
  violations, never fixes them itself, even trivial ones.
- Every pass/fail claim must be backed by live execution (request/response,
  log line, before/after count) — a source-code citation is not evidence.
  **The gate report carries that evidence as an artifact, inline or by
  reference** (`~/Harmonic_Projects/testplan/{issue}/` for anything too
  large to paste), not just prose asserting it happened — see
  `rules/testing-gate.md` rule 3 for the full requirement.
- **Fast-fail on external blockers.** If a live check is blocked by a
  genuine external dependency — a bug in another open issue this ticket's
  verification requires, a missing precondition, an environment gap that
  isn't this ticket's to fix — Lane 3 confirms the blocker is real with
  minimum evidence (one clean repro, not exploratory workarounds), then
  stops and reports it as a gate finding **immediately** — never routing
  around it, mocking past it, or retrying first. Real incident, HRSE2
  #204: Lane 3 tried several workarounds (including a mocked-browser
  test) before finally surfacing a blocker that had *already been fixed*
  by the time the report was filed — Marc: *"L3 burned 3% of my credits
  trying all kinds of crazy work-arounds instead of admitting it was
  blocked... fast-fail... is doctrine in software development."* Distinct
  from the auto-fix rule above (Lane 3's own test-spec issues) — an
  external blocker in another lane's work is always an immediate
  stop-and-report, never something to route around.

### Observe-and-report — a gate fixture only a writing lane can create (harmonic-forge#401)

**When a gate requires an artifact only a writing lane can create, Lane 1
creates the artifact and Lane 3 observes it.** The verification role stays
with Lane 3 — it still reaches its own verdict, on evidence it did not
manufacture; only the fixture's creation moves. Distinct from a Tooling
Exception (which collapses the lanes into a single implementer): this
splits one action across two lanes while the gate's independence stays
intact.

Real incident, hrse#1343: TC4 required proving GitHub's own required-check
behavior — a step reporting `skipped`, not `success` — unverifiable
without a real PR. Lane 3 is absolutely barred from creating one
(`block_lane1_status_claims.py`'s Lane 3 write-scoping denial fired
correctly, before any file was touched). Lane 1 opened
`__gate__/h1343-tc4` and PR #1346; Lane 3 observed the delete test
reporting `skipped` and `verify` reporting green.

**Four conditions, all required** — without them this is Lane 1 doing as
it pleases and calling the result a fixture:
1. `__gate__`-prefixed branches — recognizable as gate residue, matching
   the existing ledgered-fixture convention.
2. Trivial, reversible, never-merged changes — no behavior change in any
   file.
3. Closed and branches deleted afterward, including on failure.
4. Lane 3 records the underlying signal, not merely the downstream
   status — a step skipped for the wrong reason is indistinguishable
   otherwise.

**The boundary on the boundary: applies only when the artifact is
genuinely unproducible by Lane 3.** A gate that merely finds it easier to
have Lane 1 write something does not qualify — this must not become a
convenient route around the prohibition. A PR qualifies because it is the
only thing GitHub will evaluate a required check against; most gate needs
do not reach this bar.

## Per-Lane Working Directories — git worktree

Each repo a lane touches has a dedicated `git worktree` per lane
(`<repo>-lane2/`, `<repo>-lane3/`, sibling to the main checkout), sharing
one `.git` (history/objects/remotes) so there's no clone duplication —
just filesystem isolation (hrse#278/#332, real incident 2026-07-19: a
`cymagraph-infra` shared checkout went dirty mid-Lane-3-gate because Lane
2 was implementing a different issue in the same directory at the same
time). **Lane 2 always operates in `<repo>-lane2/`; Lane 3 always in
`<repo>-lane3/`; Lane 1 and the human operator use the main checkout.** A
lane starting work in a repo without its dedicated worktree existing yet
should stop and ask, not fall back to the shared main checkout.

This isolates the *filesystem*, not concurrent *live infrastructure*
mutation (two lanes can still collide applying to the same live cluster
at the same time) — Lane 3's read-only-except-human-attended-mutations
boundary already covers that risk separately.

**The normal way to start a lane session** (harmonic-forge#142) is one of
the `lane1`/`lane2`/`lane3` launcher scripts (`tools/lane/`, symlinked
onto `$PATH`) rather than a bare `claude`/other-CLI invocation. Each
script, run from anywhere inside a project's worktree tree: derives the
project's base name from the current worktree (`git rev-parse
--show-toplevel`, stripping a trailing `-lane<N>` suffix if present) and
`cd`s into the right sibling directory before launching — `lane1` into
the bare `<project>` checkout, `lane2`/`lane3` into `<project>-lane2`/
`-lane3`, refusing to launch (not falling back to the main checkout) if
that worktree doesn't exist yet; sets the `LANE` environment variable
(see below); wraps the session in `systemd-inhibit --what=sleep:idle` so
a long batched or unattended run isn't suspended mid-work; and builds the
launch command per-CLI from the closed agent registry (see below). All three
launchers share one implementation, `tools/lane/_cli_launch.sh` plus
`tools/lane/_agent_registry.sh` and `tools/lane/_lane_args.sh`, sourced the
same way `_gh_config_dir.sh` is — before harmonic-forge#318 the same block
was duplicated inline in each of the three. **`lane3` additionally refuses
to start on unrepaired drift rather than repairing it** (harmonic-forge#322
AC5, below).
**Proper hygiene is restarting the
session with the right script, never redirecting a running session into
a different lane role mid-conversation** — `LANE` is fixed for a
process's entire lifetime by design (see below), so there is nothing a
running session could do to change it even if asked to.

#### Per-CLI launch wiring — `laneN --agent`

**`--agent claude|codex|gemini` is the canonical interface** (ADR-007 § 3,
implemented in harmonic-forge#322). Native CLI arguments follow, optionally
after a bare `--`:

```bash
lane2 --agent codex                    # start Lane 2 under Codex
lane1 --agent gemini -- -p "a prompt"  # native args after --
lane3 --ack-stale "gating PR #123"     # Lane 3 only; see below
```

`tools/lane/_agent_registry.sh` is the **closed registry** and the single
place that knows what each agent needs — a version floor, a per-lane policy
slot, an operator-facing display name, a default flag. An unrecognized
`--agent` value is a hard error that execs nothing.

| agent | what the launcher injects | why |
|---|---|---|
| `claude` (default) | `--permission-mode auto` (override with `LANE_PERMISSION_MODE`, or pass `--permission-mode` explicitly) | harmonic-forge#179 |
| `gemini` | `env -u GOOGLE_API_KEY -u GEMINI_API_KEY GOOGLE_CLOUD_PROJECT=hrse-497421 …`, plus `--admin-policy` at Lanes 1 and 2 | harmonic-forge#318, #362 |
| `codex` | nothing — bare passthrough | flag injection broke Codex's own argument parsing (harmonic-forge#179) |

`LANE_AGENT` is exported alongside `LANE`, and both are fixed for the
session's lifetime by the same mechanism (see § Lane role signal below —
process-environment inheritance, not `readonly`, which does not survive
`exec`).

**`LANE_CLI` is retained for aliases only** — `claude-api`, `claude-pro` —
and resolves to its agent by prefix against the same closed list. **Passing
both `--agent` and `LANE_CLI` is an error, not a precedence question**
(ADR-007 § 3: an operator who set `LANE_CLI` in a shell profile and then
typed `--agent gemini` would otherwise get a session that ignores half of
what they said and reports nothing). A `LANE_CLI` matching no registered
agent is likewise refused rather than execed: before harmonic-forge#322 it
fell through to bare passthrough and silently received no policy injection
and no version floor.

**Minimum versions** (AC9) are floored at the *minor*, not the patch:
`claude >= 2.1`, `codex >= 0.150`, `gemini >= 0.56`, recorded against the
exact patch versions each was qualified at (`2.1.250` / `0.150.1` /
`0.56.0`). A patch-pinned floor would false-alarm on every routine `npm -g`
update while duplicating the parity suite's own version bookkeeping
(harmonic-forge#325); ADR-007 already handles version-specific qualification
there.

#### `lane3` performs zero mutations — and `lane3-provision`

The gate launcher used to `git fetch origin main` and `ln -sf` the
worktree's `backend/.env` before starting the session. It no longer does
either (harmonic-forge#322 AC5), for one reason: **a gate that repairs its
own preconditions cannot report on them.** A worktree silently brought up to
date at launch is indistinguishable, in the gate's own report, from one that
was never stale.

Both protections survive as **checks** — neither was dropped (AC6):

| drift | detected how | on drift |
|---|---|---|
| worktree behind `origin/main` (harmonic-forge#255) | `git ls-remote origin refs/heads/main`, which writes no refs; then `git cat-file -e` on the returned SHA, then an ancestry comparison against that SHA — never against the stale local `origin/main` ref | **refuse**, with `--ack-stale "<reason>"` as the escape hatch |
| `backend/.env` not linked to the main checkout's (harmonic-forge#264) | symlink target comparison; never reads the file's contents | **refuse, no escape hatch** |

The asymmetry is deliberate. A Lane 3 session gating a deliberately-older
target branch is not behind by mistake, so staleness has a legitimate form
and the operator states it once, in writing (an empty reason is rejected,
following `l1_post.py`'s `--ack-overlap` precedent). `backend/.env` has no
legitimate per-worktree divergence at all, so there is nothing to
acknowledge — and a gate run against a stale env silently loses its live
HTTP/auth test surface (hrse#792).

Both mutations now live in **`lane3-provision`**, run deliberately and
separately:

```bash
lane3-provision   # fetch + check out origin/main, relink backend/.env
lane3             # then start the gate
```

Not a `--provision` flag on `lane3`, because a flag means the same script
both mutates and does not, decided by an argument — which reintroduces the
hazard the moment it reaches an alias, a shell history, or muscle memory.

**The two Gemini unsets are load-bearing, not hygiene.** Standing operator
directive: the Gemini CLI always uses the OAuth path; `GOOGLE_API_KEY` and
`GEMINI_API_KEY` exist for programmatic use elsewhere. That is not the
default and it fails *silently* — with both keys present (the operator's
normal shell state) Gemini CLI prints `Both GOOGLE_API_KEY and
GEMINI_API_KEY are set. Using GOOGLE_API_KEY` and runs on the API-key
identity and quota, with no error and correct-looking output. The unsets
are process-scoped and never touch the operator's interactive shell — but
they are inherited by the session's entire subprocess tree, so a service
started from inside a Gemini lane session also sees `GEMINI_API_KEY` absent
(it is a live HRSE2 backend variable). A self-reported "unconfigured" from
inside such a session is expected, not a defect.

`GOOGLE_CLOUD_PROJECT` is required because the active account is
Workspace-managed; Workspace identities must name a GCP project for
Google's Code Assist path, and personal `@gmail.com` accounts need not.
Without it, every invocation dies with `ProjectIdRequiredError` before any
tool call. `hrse-497421` is a project id, not a credential — it is
committed deliberately. Export `GOOGLE_CLOUD_PROJECT` yourself to override.

Two traps that cost a full diagnosis cycle in harmonic-forge#318:

1. While `~/.gemini/settings.json` pins
   `security.auth.selectedType = "oauth-personal"`, a `GEMINI_API_KEY` in
   the environment is ignored entirely. Its presence is not evidence auth
   works; its absence is not evidence anything is broken.
2. **A passing `gemini -p` is not evidence the OAuth path works** unless
   the keys were explicitly unset for that run — the API-key path produces
   identical-looking output.

#### The adapter contract — what "supported" means

Which agent may fill which lane is governed by
[`docs/decisions/ADR-007-multi-agent-adapter-contract-and-capability-tiers.md`](docs/decisions/ADR-007-multi-agent-adapter-contract-and-capability-tiers.md),
not by whether the launcher will start it. Three properties from that ADR
are operative here:

1. **The enforcement point is the launcher, not the repo.** An agent
   started outside `laneN` carries no lane enforcement at all — Codex's
   `--sandbox read-only` and Gemini's `--admin-policy` both arrive as
   launch flags. A repo-tracked policy file is reviewable, not active.
2. **Directive prose is never enforcement.** A rule in a directive file
   tells an agent what not to do; a hook or a policy prevents it. The same
   corpus is a hard boundary under Claude Code and nothing at all under an
   agent that never loads the hooks.
3. **Capability tiers are per-capability, not per-agent.** Qualified for
   Lane 3 tier 1 confers nothing about tier 2, and a qualification is
   against a specific CLI version.

**Current state: Gemini is approved for Lane 1, partially for Lane 2, and for
Lane 3 tier 1 (static review) only** (harmonic-forge#318/#362/#326). #362 landed an admin-tier policy
(`tools/lane/policies/gemini-lane1.toml`/`gemini-lane2.toml`, wired through
`_cli_launch.sh`'s `gemini*` branch) that structurally denies `write_file`/
`replace` for Lane 1 (role boundary — Lane 1 never edits files) and
`activate_skill`/`invoke_agent` for both lanes — proven live, not merely
schema-valid, by `tools/lane/policies/canary/run_canary.py`. **Lane 2's
`run_shell_command` is deliberately left fully open** — an `argsPattern`/
`commandPrefix` deny-list on an otherwise-unconstrained shell was found live
(harmonic-forge#362's pitch-inspection) to leave the tool visible and be
bypassable via one level of shell-wrapper indirection, the identical class
of gap `block_irreversible_ops.py`'s own docstring already concedes and
rules out chasing for Claude. **`gh issue close`/`gh pr merge` are therefore
still demonstrably reachable from a Gemini Lane 2 session** — `batch_auth.py`
fails *open* there exactly as before, and this is recorded, not silently
assumed fixed. See ADR-007 § 7/§ 8 for the full guard-equivalence matrix and
acceptance-tier detail, which must be updated in the same change that adds
any new hook. Both policies are now **un-removable via a passthrough `--admin-policy`**
(harmonic-forge#322 AC4): the launcher rejects the flag outright at any lane
whose registry slot declares a policy, rather than appending `"$@"` after it
and letting last-flag-wins decide. That closes the launcher-side half of the
dependency F326 carries — it does not make the policy a boundary the model
cannot reason past, which is a separate claim this platform deliberately does
not make (ADR-007 § 9).

#### Gemini Lane 3 — tier 1 (static review) only

`tools/lane/policies/gemini-lane3.toml` (harmonic-forge#326), armed by
`AGENT_LANE_POLICY[gemini:3]`. **Tier 1 is static review: no test execution,
no live services, no migrations** — tiers 2–4 are harmonic-forge#327's and
remain blocked.

It denies **whole-tool** the seven tools verified live to be both registered in
a headless session and capable of mutation, egress, or nesting:
`run_shell_command`, `write_file`, `replace`, `web_fetch`, `google_web_search`,
`activate_skill`, `invoke_agent`. It carries **no argument-scoped rule at all**,
which is the substantive difference from the Lane 1 policy above and the
lesson of harmonic-forge#412: a `commandPrefix` allowlist on
`run_shell_command` is not a boundary, because redirection is only downgraded
to `ASK_USER` (and that downgrade is off under `--yolo`), and a native write
flag such as `git diff --output=<path>` contains no shell metacharacter for any
pattern to catch.

**A Gemini Lane 3 session fetches only its bounded context through the
system-installed `lane3-context` MCP extension (harmonic-forge#1414).** The
normal launcher remains the sole operation:

```bash
LANE_CLI=gemini lane3
```

After receiving `Test H<N>` or `Test F<N>`, Gemini calls the sole permitted
tool, `lane3-context.fetch_context`. It accepts only that typed issue id,
requires the prefix to match the active Lane 3 worktree's canonical remote,
then returns `fetch_lane1_context.py`'s Lane-1-only output plus the fixed
`origin/main...HEAD` diff and target SHA. It accepts no command, URL, path,
ref, target SHA, or raw GitHub endpoint. Shell, raw network/GitHub, writes,
and every other MCP tool remain denied. This is Tier 1 context only; it grants
none of harmonic-forge#327's executable-test, service, browser, or migration
authority.

**The tier is qualified only by a passing run of
`tools/lane/policies/canary/run_canary.py`, executed by Lane 3** — not by the
author of the policy. Re-run it on every Gemini CLI upgrade; ADR-007's reading
rule is that a cell is qualified against a version, and an upgrade invalidates
it until the suite is re-run.

### Lane role signal — `LANE`

`LANE` is a real OS-level environment variable, set once by the launcher
script at process launch, inherited by that session's entire subprocess
tree — including every `PreToolUse` hook subprocess Claude Code spawns
for that session (verified live, harmonic-forge#142), and, separately,
every hook subprocess Codex spawns for its own sessions via its own
`shell-LANE=3`-style propagation path (verified live, harmonic-forge#148
— Claude Code and Codex each inherit `LANE` into their subprocess trees,
by their own separate mechanisms, not one shared implementation). Project
hooks read it directly (e.g. HRSE2's `scripts/block_lane1_status_claims.py`,
canonicalized at `harmonic-forge/tools/hooks/`, harmonic-forge#149) to
mechanically enforce lane-specific constraints: denying Lane 2 writes
into the main checkout (harmonic-forge#142), denying Lane 3 writes
outside `~/Harmonic_Projects/testplan/` (harmonic-forge#150), gating the
`gate-*` mise tasks (harmonic-forge#151), and gating Codex's own
`scripts/gate_codex_tool.py`'s `mise run gate-*` dispatch on the same
`LANE` signal (harmonic-forge#152, extended by harmonic-forge#190).

**What `LANE` is not**: not adversarial, not a hard security boundary,
not inferred from conversation text, and not something a session
declares about itself mid-session. It exists specifically because two
earlier designs for the same class of guard both failed real review:

1. A self-declared marker file (a session runs a `*-begin` task to mark
   itself) — filesystem-global with no session-identity check meant one
   session's marker could block an unrelated session's legitimate work,
   and being opt-in meant it never caught the session that skips a
   convention it already knows.
2. A marker armed by pattern-matching the operator's own trigger phrases
   in chat — trigger-shaped text turned out to be unavoidable in normal
   Lane 1 status-relay conversation (proven live: the message that
   triggered a review of this exact design matched its own trigger
   pattern), with no worktree to fall back to for the session it
   falsely armed.

`LANE` set at process launch has neither failure mode: it can't be
mistyped mid-conversation, misread from prose, or left stale by another
session, because it isn't shared state at all — it's a fact about how
one specific process was started, visible only to that process's own
children.

### Per-Issue Implementation Worktree — distinct from the fixed per-lane worktree above

Lane 2's actual *implementation work for a given issue* happens in a
separate, disposable `/tmp/<repo>-<issue>-impl` worktree, created fresh
per issue from the fixed `<repo>-lane2/` session worktree above — never
implemented directly in `<repo>-lane2/` itself. Full create/provision/
work/cleanup procedure: see the `impl-worktree` skill.

Two facts the skill doesn't carry, kept here because they're enforcement
and launcher-scope facts, not procedure: **`block_lane1_status_claims.py`'s
Lane 2 denial (a write into the main checkout while `LANE=2`) is the
single live enforcement point for this convention today** — not the
launcher scripts. And **`tools/lane/lane2`/`lane3` have zero scripted
awareness of the per-issue impl worktree** — creating and removing it is
a purely manual, session-driven step.

## Per-Lane Worktree Reuse Across Issues — Check Before You Checkout

Per-lane isolation (above) is per-*lane*, not per-*issue*: `<repo>-lane3/`
is a single fixed directory reused sequentially across every issue Lane 3
gates. Nothing stops a second actor from checking out a *different*
branch into that same directory while a live process (dev server, running
test) from the first actor's session still has its cwd there — `git
worktree` only blocks checking out the same branch twice, not a different
one (harmonic-forge#137, real incident hrse#439, 2026-07-30: a second
session checked out and rebased a different issue's branch in
`HRSE2-lane3` while Lane 3 was actively gating hrse#439 there, disrupting
the live gate).

**Before switching branches in a shared per-lane worktree, check for live
processes with cwd inside it.** `gate-checkout` does this automatically
(`tools/worktree/check_worktree_busy.py`, walking `/proc/*/cwd`) and
refuses the checkout if anything other than the caller's own process tree
is running there — always use `mise run gate-checkout <branch>` for this,
never a bare `git checkout`/`git rebase` in a shared lane worktree. If it
refuses: wait, ask the operator, or do prep work (rebase, conflict
resolution) in a disposable scratch worktree (e.g. `/tmp/<repo>-<issue>-prep`)
instead, only touching the shared lane worktree once it's confirmed idle.

## Shared Working Directory — Commit Before You Yield

Moved to `universal-agent.md`'s "Shared Working Directory — Commit Before
You Yield" section (harmonic-forge#170) — this is a cross-lane discipline
rule, not protocol-specific, and applies identically to every lane/tool.

## HITL Gate Language

Lane 2 and Lane 3 sessions (either CLI) post their own results directly to
the GitHub issue thread; Lane 1 (or a session with no `LANE` set) routes
through `mise run l1-post`/`lane-comment` instead (harmonic-forge#190/#191/#193 —
before that fix, Codex Lane 3 always posted directly while Claude Lane 3
was unconditionally blocked, an accidental asymmetry, not a design). This
lets the human
operator (HITL) drive the loop with short, canonical trigger phrases
instead of relaying pasted content between tools. Every trigger names the
issue explicitly (`#N`) — multiple issues can be in flight at once, and an
unnumbered trigger is ambiguous. Some triggers go to Lane 1; others go
directly to Lane 2 or Lane 3 in their own respective interfaces, and Lane 1
never sees them or acts on them.

**The only channel between lanes is a GitHub issue comment HITL relays.**
No lane contacts another lane's live session directly — cross-session
messaging or any HITL-bypassing mechanism, regardless of content or
urgency; seeing another session is fine, addressing it is not. Deny it at
the tool-call level where the repo can — but a hook only protects a
session whose settings wire it (hooks distribute by hand, unlike this
file), so treat this as the standing rule until it does everywhere, not
as a solved problem.

**Repo-prefixed issue numbers, when more than one repo is in flight.**
Once a collaborator works across multiple repos in the same session, a bare
`#N` is ambiguous the moment both repos happen to have an open issue with
the same number — a real incident, HRSE2 2026-07-18: a status update named
`#26`, and the two repos' issue `#26`s were entirely unrelated work.

**The prefix table is in `rules/lane-shorthand.md`**, which loads in every
session. It is not duplicated here: two copies is how the vocabulary
drifted (harmonic-forge#289). It also carries the parts a list cannot —
that `L` is permanently reserved, that the set is derived from
`isArchived` rather than hand-maintained, and that a cross-account prefix
returning empty means wrong credentials, never absence.

**Universal, not just for triggers (extended 2026-07-19).** Every issue
reference — a status update, a summary, a plain mention in a comment or
in chat, not only the trigger phrases above — carries the `H`/`F`/`I`/`O`
prefix, always, with no "obviously which repo from context" exception. HITL
often relays an agent's own issue references directly into commands sent
to other lanes; an unprefixed number forces HITL to disambiguate it
first, which defeats the convention's purpose. "Closed #313" is wrong;
"Closed H313" is right, in a summary just as much as in a trigger.

**Lane-status shorthand.** A quick status update, not a trigger. **The
token table is in `rules/lane-shorthand.md`**, which loads in every session;
it is not duplicated here (harmonic-forge#289). Note `L2P` is **retired** in
favour of `L2S`, and `L2S`/`L3S` are review requests directed at Lane 1
rather than terminal outcomes — a session that reads them as outcomes waits
instead of acting.

A status token is a pointer to go read that lane's actual report on the
issue thread, never a substitute for it — the verification standard above
applies in full.

**`B` is not a failure verdict, and it is available on every lane** (`L1B`,
`L2B`, `L3B`) — a lane reporting BLOCKED is the protocol working. The
correct response is to fix the blocking condition and re-run, **not** to
route the work back a lane. `L3F` and `L3B` are different outcomes and
conflating them sends the wrong work to the wrong lane:

- **`L3F`** — the gate ran and something failed. The *implementation* is in
  question. Routes to "Reimplement #N".
- **`L3B`** — the gate could not produce a trustworthy verdict at all. The
  implementation is *not* what is in question; a precondition, the
  environment, or the baseline is. Routes to remediation, then a re-run of
  the same commit.

Real incident (hrse#847, 2026-08-13): every non-live check passed and the
implementation was sound, but a concurrently-running backend on a stale
worktree had rewritten the live baseline the gate depended on, so Lane 3
could not produce a verdict it could stand behind and reported `L3B`.
Reporting that as `L3F` would have implied the fix was wrong and sent it
back to Lane 2 — the wrong lane, and work that did not need doing. The
environment defect was tracked separately (hrse#863). `L2B` has the same
shape: on hrse#849 Lane 2 twice self-blocked — once on a Plan-First gate
not yet passed, once on being asked to run a data-modifying script it is
categorically not authorized to run — and both refusals were correct.

`AE` (approved, execute) — the operator's go-ahead for Lane 3 to run the
TCs in an already-approved test spec, distinct from approving the spec's
content (that's the `L3S` → HITL-approval step itself). Like every other
trigger phrase in this section, `AE` must be posted as an actual issue
comment, not only said to Lane 1 in chat — Lane 3 verifies it
independently on the thread before executing any TC, exactly as it
already verifies the gate-readiness sweep. **`AE` and its sweep are one
atomic action, same turn, sweep strictly after** — use the repo's own
atomic wrapper where one exists; otherwise confirm the comment order
before telling HITL either is ready.

**A routine retest after a FAIL does not need a new AE/sweep pair.**
HRSE2's `check_lane3_ready.py` (hrse#1102, generalized hrse#1359) already
carries the original AE/sweep's authorization forward onto a new SHA when
Lane 1 posts a `ready-for-l3` naming that SHA after the original
authorizing comment — the `ready-for-l3` alone is sufficient re-authorization
for the fix-and-repush cycle. Reserve a fresh AE + sweep for when the
*plan* itself changed (a reforged approach, new scope, or a sticky-wicket
reforge) — posting one on every SHA bump regardless is unnecessary
overhead this mechanism already exists to avoid (harmonic-forge#425).

**An AE may widen what a gate's write tier covers; it may never waive a
lane's absolute role prohibition** (harmonic-forge#401) — those are
different boundaries, and only the first is HITL's to grant through this
trigger. When a gate needs an artifact only a writing lane can produce,
see § Lane 3's observe-and-report pattern above rather than authorizing
the barred lane to produce it directly.

**Before acting on any trigger, the receiving lane checks the issue's
state.** If `#N` is already closed, stop and ask HITL to confirm the
number before doing anything — don't proceed on the assumption a closed
issue was meant. A typo'd issue number (fat-fingering a digit, or naming
one that was just closed a few messages ago) is an easy, low-cost mistake
to make in a fast-moving session with many issues open at once, and
proceeding against the wrong issue wastes a full lane cycle and produces
a confusing record on the wrong thread. This applies to whichever lane
receives the trigger — Lane 1, Lane 2, or Lane 3 alike.

**Every trigger naming an issue — including a bare "continue"/"proceed" —
means a full, unfiltered re-read first**, body and every comment; a
repeated trigger is often the signal something changed, not evidence it
didn't. Enforce at the trigger phrase itself where the repo can, not by
recall.

1. **Lane 1** diagnoses and posts a handoff comment on issue #N (and
   displays it in chat). No trigger needed — this happens unprompted once
   diagnosis is done.
2. **HITL says "Implement #N"** (→ Lane 2). Lane 2 fetches the issue and
   Lane 1's handoff comment from GitHub itself and implements it, posting
   its own completion report as a comment on #N. **For a plan-first issue
   (see § Plan-First Implementation), this trigger is never sent first —
   HITL sends "Plan #N" instead** (step 2a below); "Implement #N" for that
   issue only follows a PROCEED/PROCEED WITH NAMED CHANGES verdict. Real
   incident this fixes: on HRSE2 #236, the handoff's own bolded stop
   instruction was overridden in practice because "Implement #N" — the
   literal, most recent go-signal — contradicts a "but plan first" caveat
   living in the same comment. Two distinct trigger words remove the
   contradiction instead of relying on Lane 2 resolving it correctly every
   time (see ADR-005).
   - **2a. HITL says "Plan #N"** (→ Lane 2, plan-first issues only). Lane 2
     fetches the issue and Lane 1's handoff — which for a plan-first issue
     contains no Implementation Spec section yet (see § Plan-First
     Implementation) — and posts its implementation plan as a comment,
     then stops. There is nothing to implement from yet, so nothing to
     skip ahead into.
   - **2b. HITL relays "Plan up for #N"** (→ Lane 1, plan-first issues
     only, once Lane 2's plan comment from step 2a exists). Lane 1 invokes
     `pitch-inspection` in plan-review mode and posts the verdict, then
     (if PROCEED/PROCEED WITH NAMED CHANGES) posts the withheld
     Implementation Spec as a follow-up comment — see § Plan-First
     Implementation for the full process. Only after that does HITL send
     "Implement #N".
3. **HITL says "Lane 2 done for #N"** (→ Lane 1). Lane 1 checks the **full
   working-tree diff** (`git status` / `git diff` across the whole repo,
   not just the files the handoff predicted) against the handoff's
   affected-files list and acceptance criteria — flagging scope creep for
   *anything* touched outside what was specified, including new untracked
   files. Checking only the predicted files is confirmation bias, not
   review, and it has already let a real violation through once (#154 —
   Lane 2 created and ran an unauthorized data-modifying script that a
   predicted-files-only diff never surfaced). Lane 1 also independently
   spot-verifies at least one significant behavioral claim live — never
   just re-reads Lane 2's own description and calls it confirmed. **This
   spot-check stays to fast, deterministic, read-only tools (a curl call,
   a direct DB query, lint/build/mypy, a log grep) — it never runs a
   formal, interactive test suite that was built for Lane 3's own gated
   execution** (e.g. a Playwright suite once one exists). The moment a
   piece of tooling is built as *a lane's designated execution
   capability*, running it from another lane — even just to "spot-check" —
   does that lane's actual job instead of a lightweight check on it. This
   happened for real: Lane 1 ran the full Playwright suite #159 had just
   built (which exists specifically to give Lane 3 real interactive
   verification) as part of a routine "Lane 2 done" review, diluting the
   reason Lane 3's independent, HITL-gated execution exists. Same family
   as the Lane 2/Lane 3 data-execution-authority split above — once a
   capability is designated to one lane, another lane doesn't borrow it
   for convenience, no matter how tempting it is when the tooling is new.
   - **If a problem is found:** Lane 1 posts a correction comment on #N
     specifying exactly what Lane 2 needs to fix, and reports this to HITL
     instead of proceeding. Once addressed, HITL re-triggers with
     **"Reimplement #N"** (distinct from the first-pass **"Implement #N"**
     — this word specifically signals a correction loop, not new work),
     looping back to step 2.
   - **If confirmed clean:** Lane 1 drafts a second comment addressed to
     Lane 3 (carrying any caveats the review turned up) and posts it to
     #N, then tells HITL it's ready.
4. **HITL says "Test #N"** (→ Lane 3). Lane 3 fetches the issue body,
   Lane 1's original handoff comment, and Lane 1's Lane-3-addressed
   comment from step 3 — never Lane 2's comment — via
   `harmonic-forge/tools/gh/fetch_lane1_context.py` (harmonic-forge#254),
   never a manual `gh` read of the full thread, which exposes Lane 2's
   comment before a session can self-censor it (the repeat-violation
   pattern on vitalharmony/hrse#793 this script exists to close
   structurally). Lane 3 derives a test spec from that output and submits
   it for HITL approval (`templates/hitl-test-review.md`) before executing
   anything. After approval, Lane 3 executes and posts its gate report as
   a comment on #N.
5. **HITL says "Lane 3 done for #N"** (→ Lane 1). Lane 1 reads #N's Lane 3
   gate comment, confirms every claim is backed by live execution (not
   source-code reasoning) **by inspecting each check's attached evidence
   artifact, not by trusting the prose claim** (see `rules/testing-gate.md`
   rule 3), checks for protocol adherence (Lane 3 avoided
   Lane 2's comment before writing its spec, and did not implement any fix
   itself during its style/refactor pass — see `rules/testing-gate.md`),
   and independently spot-verifies at least one claim live if anything is
   surprising, high-stakes, or contradicts prior known state.
   - **If a problem is found:** Lane 1 reports the specific issue to HITL
     with a recommendation on whether it routes back to Lane 2 (an
     implementation bug — HITL says **"Reimplement #N"**, loop to step 2)
     or requires Lane 3 to re-test (a gate/spec issue — HITL says
     **"Retest #N"**, distinct from the first-pass **"Test #N"**, loop to
     step 4). HITL decides which.
   - **If confirmed clean:** Lane 1 recommends closing — never a
     unilateral close.
6. **HITL says "Close #N"** (→ Lane 1). Lane 1 posts a closing summary
   comment (referencing the gate evidence) and closes the issue. This is
   the only trigger that results in a close, and it is never self-initiated
   — **by any lane**, not just Lane 1, regardless of how clean a prior gate
   or implementation looked. This was originally written naming only Lane 1
   (after HRSE2's `#149` incident); that narrower wording let a real Lane 2
   violation slip through on HRSE2 `#186` — Lane 2 implemented a fix and
   closed the issue itself, with no Lane 3 gate having run at all. The rule
   is: closing requires the human operator's explicit "Close #N," every
   time, from every lane, with no exception for confidence or a clean local
   test pass.

   **A merge-time break routes back to step 2 unless Lane 1 can show —
   not assert — all three:** every gated file hash-verified unchanged, the
   full suite re-run against the known baseline, and any changed assertion
   broken deliberately two ways and restored. Short of that, it isn't
   mechanical.

## Tooling Exception — Dev/Test Tooling Skips the Full Loop

The full Lane 1 → Lane 2 → Lane 3 cycle exists to protect code that ships.
Dev/test tooling does not ship, and routing it through the full loop
multiplies every one-line bug into a full gate cycle. Real incident: HRSE2
#233 (parity-test-suite authoring) burned roughly 10 rounds and a full
day's credit budget, with the per-round cost dominated by loop overhead
(a full independent Lane 3 suite re-run per trivial fix), not by the bugs
themselves. Full incident and decision record:
`docs/decisions/ADR-002-tooling-vs-application-3-lane-exception.md`.

**Scope — ALL of the following must hold, or the full loop applies:**
1. The work product is a development/test/verification harness, migration
   script, or repo-local automation — it is never imported by, served by,
   or deployed with application code, and no production or user-facing
   path can execute it.
2. Its blast radius is limited to the dev machine and repo working state,
   and any mutation it performs there is contained by its own design
   (disposable branches, snapshot/restore, scratch dirs outside the
   tracked tree).
3. The operator has explicitly scoped the specific issue as tooling work
   (label `infrastructure` or `tech-debt` plus an explicit note in the
   issue body). This exception never defaults open; when in doubt, it is
   application code. Anything that touches application source, schemas,
   CI that gates merges, secrets handling, or data in the graph is NOT
   tooling work regardless of where the file lives.

This is the project-level analog of the platform-tooling exception in
`rules/universal-lane1.md` ("The operator may scope platform-level
tooling/documentation work directly to Claude Code."), and carries the
same posture: explicit, per-issue, never assumed.

**Process under the exception:**
- **Single implementer.** One agent (whichever lane the operator assigns,
  including Lane 1/Claude Code as an explicit exception to "Lane 1 never
  implements") designs and writes the tooling in one pass. No Lane 1
  handoff document, no Lane 2 relay, no per-round Lane 3 gates.
- **One human-reviewed pass, before commit.** The operator (or a
  designated reviewer who is not the implementer) reviews for exactly
  three things: (1) scope — it is genuinely tooling per the boundary
  above; (2) containment — the mutations it performs on the dev
  machine/repo are bounded and reversible; (3) honesty — its checks
  verify live behavior, not prose claims (`rules/testing-gate.md` still
  applies to what the tooling *asserts*, even though the tooling itself
  skips the gate).
- **Even under this exception, the implementer never grades its own
  verification.** If the implementer is Lane 1, a fix Lane 1 just wrote is
  not verified by Lane 1 running it and declaring success — that is the
  same maker-grades-own-work failure the whole 3-lane structure exists to
  prevent, just relocated inside a single lane. Get a second read (a
  fresh-context subagent, or the human reviewer above) before trusting the
  result, especially under time or cost pressure — that is exactly when
  the shortcut is most tempting and least reliable. Real incident, same
  night as #233: Lane 1 wrote a one-line fix, ran it through the suite
  itself without independent review, and had gotten the fix backwards —
  caught only because `sticky-wicket` was invoked afterward. See ADR-002.
- **Lane 3 still gates the moment the tooling's output matters.** The
  tooling's *first consequential use* — the run whose result approves or
  blocks shipping work — is a Lane 3 gate with the standard evidence
  rules. Lane 3 does not re-gate the tooling itself once it has passed its
  one human-reviewed pass; a defect discovered at that later gate is a
  finding against the tooling, handled under this same exception (fix it,
  one more human-reviewed pass, no return to the full loop).
- **An issue modifying Lane 3's own gate enforcement is
  Tooling-Exception-eligible by default, stated at filing time** — a
  normal gate run is itself a gate-profile session, so testing new gate
  rules needs the old rules to already permit them: a bootstrap problem,
  not an allow-list patch.
- **`preclose-inspection` is that second read, and it is now enforced
  rather than remembered (hrse#1487).** Run it on the diff that is about
  to be merged, act on its findings, then record that it ran:

  ```
  gh issue edit <N> --repo <owner/repo> --add-label preclose-inspected
  ```

  `tools/hooks/block_missing_preclose_inspection.py` blocks `gh pr merge`,
  `gh issue close`, and `gh api PATCH … state=closed` on an issue labelled
  `tooling-exception` that does not yet carry `preclose-inspected`.

  **It is opt-in, on `tooling-exception`** — like the data-migration close
  gate it is modelled on, and unlike its own first two implementations,
  which gated on the *absence* of a Lane 3 trail and therefore fired on
  every unlabelled issue in both repos. A "not planned" close has no diff
  to inspect, and a gate whose only escape is asserting a review that never
  ran corrodes the signal it depends on. So label the issue
  `tooling-exception` when scoping it under this exception — that label is
  what arms the gate.

  The gate reads **labels, not comment text**: a marker whose format must
  be published is a valid credential wherever it is published, which is why
  naming `preclose-inspected` here — or in the hook's own deny message —
  does not grant it.

  Real incident, hrse#1476 (2026-09-01): Lane 1 implemented, verified,
  pushed, and went straight to merge/close, skipping the review entirely.
  It only ran because the operator asked afterward, and then found five
  defects, one of which would have shipped the feature inert and green.

## Escalation

Any lane escalates to the Tech Lead (human) rather than guessing, looping, or
silently narrowing scope, when:
- The issue spec is ambiguous (Lane 1).
- The handoff itself is unclear or contradicts `.windsurfrules` (Lane 2).
- The same root cause survives 3 fix attempts (Lane 3), or a check cannot be
  live-verified in the current environment (Lane 3 — report the gap, don't
  fake the check). **This covers repeated tool/command failures during test
  authoring, not just failed fix-attempts on a specific assertion** — if a
  command fails, hangs, or requires manual intervention 3 times in a row for
  the same underlying reason, that's the same signal as 3 failed fix
  attempts and escalates the same way. Real incident: Lane 3 hit repeated
  bad Playwright command failures while exploring how to interact with the
  app for #152, requiring Marc to manually cancel the terminal multiple
  times, well past the point escalation should have fired.

### Cross-lane thrashing — the "sticky wicket" circuit breaker

The rules above cap retries *within* one lane's single attempt. A different
failure mode is cross-lane: the same **issue** cycles Lane 2 completion
claim → Lane 3 gate FAIL (or Lane 1 declining a completion claim) round
after round, each round fixing a real, correctly-diagnosed problem, without
the issue converging. Individually each round looks like the protocol
working correctly (Lane 3 catching real bugs, Lane 1 catching real gaps) —
the failure is only visible zoomed out, across rounds.

**Trigger (countable, not a vibe check): 2 consecutive FAIL/declined-
completion verdicts on the same issue.** At that point Lane 1 invokes the
`sticky-wicket` subagent (`agents/sticky-wicket.md`) — fresh context, no
anchoring on the round-by-round history the calling session has
accumulated — to read the full issue thread and diagnose whether the
underlying *approach* is structurally wrong, not just the latest bug. This
is the cross-lane analog of a software circuit breaker: after N failures,
stop retrying the same thing and ask whether the thing itself is broken.

**The unrelated-bug carve-out is category-level, not symptom-level.** Before
Lane 1 declines to invoke `sticky-wicket` because the two verdicts appear to
have different immediate causes, it must classify each finding at the shared
structural level (for example: evidence is not durable across runner lifetime,
credential resolution, or classification logic) and compare those categories.
Different surface symptoms or newly visible failure points do not reset the
counter when both findings belong to one structural category. If the category
matches—or the second round shows escalating effort without that category
shrinking—invoke at verdict two. Decline only when the categories are
genuinely unrelated, and state that distinction explicitly.

The threshold was lowered from 3 to 2 after HRSE2 #233: by round 3 the
thrashing pattern was already fully visible in hindsight, and every
additional round before the circuit breaker fired cost a full gate cycle.
Two consecutive failed/declined rounds on the same issue is a cheap check
that is either quickly confirmed as "normal iteration — continue" or
catches a structural problem two rounds earlier. (This is a separate
counter from the within-lane retry caps above — a single flaky command
failing twice on an otherwise-converging issue should not trip this; it's
specifically for the same issue producing two full FAIL/declined-completion
verdicts in a row.)

Real incident this rule generalizes from: HRSE2 #233 (parity-test-suite
authoring) cycled roughly 10 rounds of FAIL/declined-completion before the
pattern was named — most rounds' findings were real and correctly caught,
but the reviewing session kept re-diagnosing symptoms (a stray git branch,
a lost stash, an unresolved skip) without stepping back to ask whether the
disposable-branch test architecture itself was the source of the recurring
class of bug. The eventual `sticky-wicket` pass also caught a second-order
instance of the same failure: Lane 1's own self-verification of a fix it
had just written (skipping the subagent and grading its own work) turned
out to be wrong, reverting a correct earlier fix — see
`docs/decisions/ADR-002-tooling-vs-application-3-lane-exception.md` for the
full incident and the process change it produced.

See `agents/sticky-wicket.md` for the subagent's full operating rules.

## Pre-Flight Second Read — Catching a Bad Design Before Lane 2 Starts

`sticky-wicket` (above) is reactive: it fires after thrashing has already
happened. This is its proactive counterpart, for the same underlying
failure mode observed twice in one night during the #233 incident — the
maker (Lane 1) grading its own design, not just its own code. See
`docs/decisions/ADR-003-pitch-inspection-preflight-second-read.md` for the
evaluation that produced this (including the case against a broader
version, which was rejected).

**Trigger — self-declared by Lane 1's own handoff, not a separate risk
assessment.** `templates/lane1-handoff.md` carries two mandatory fields:
*Design Alternatives Considered* and *Load-Bearing Assumptions*. Lane 1
invokes the `pitch-inspection` subagent (`agents/pitch-inspection.md`)
before posting the handoff, whenever any of:
1. Design Alternatives Considered is anything other than "none."
2. Load-Bearing Assumptions contains any entry marked "asserted" rather
   than "verified-live."
3. The implementation's own operation mutates git state or live data
   (beyond the deliverable's normal function) **and** the issue is not
   already routed through the Tooling Exception (which has its own
   human-reviewed pass covering this).

Most handoffs — a single obvious design, no unverified assumptions, no
self-mutating automation — post with zero additional review. The two
template fields cost nothing to fill in as "none"; the second read only
fires when Lane 1 itself has flagged something contestable.

**One pass, no loop.** `pitch-inspection` returns PROCEED / PROCEED WITH
NAMED CHANGES / REFORGE BEFORE HANDOFF. Lane 1 revises once if needed and
posts. If Lane 1 disagrees with the verdict after that one revision, that
is an escalation to the human operator — never a second pre-flight round.
Building a new thrash source one stage earlier would defeat the point.

**What this does not cover:** verification-honesty failures (a lane
reporting completion that doesn't match reality) are not a design problem;
no pre-flight review touches that class. It is governed separately by the
verify-live-not-source standard.

## Pre-Handoff Precondition Trace — Stop Reactive Blocker Discovery

Real incident, harmonic-forge#93: a single command hit five consecutive
blockers across five round-trips, each one statically discoverable in the
target's own guard-clause chain before anything was sent. The trace-and-
verify pass was voluntary and memory-triggered, so it only happened once
demanded explicitly. This makes it structural instead.

**Trigger:** the handoff's receiving action is a live/`--apply`/data-
mutating command, or the deliverable's actual location differs from the
issue's tracking repo. Satisfied by `templates/lane1-handoff.md`'s
*Pre-Flight Preconditions* field — every item traced (guard-clause/flag
chain read, evidence pasted inline), verified-present (checked live in the
*receiving* environment), or external-blocked (a genuine unknown needing a
human, e.g. a live-console-only value — name it, don't force-verify it).
A blank/incomplete field on a qualifying handoff is a `pitch-inspection`
trigger, same bar as an "asserted" Load-Bearing Assumption.

**Symmetric on Lane 2's side.** Lane 2 does not trust Lane 1's field on
faith, the same way Lane 3 never trusts Lane 2's completion claims — it
re-verifies each precondition itself before writing any code, and reports
back a discrepancy rather than proceeding past one. Lane 1 traces wrong
sometimes too (see #93's own incident record).

**What this does not cover:** genuinely unknowable externals (an OAuth
console value, a third party's live state) stay external-blocked, routed
to the human — this section forces tracing what's knowable, not
verifying the unverifiable.

## Long-Running Script Handoffs — Design for Bounded Verification Up Front

Real incident, HRSE2 #456: a one-time backfill made 251 sequential network
calls and wrote its evidence artifact only when the entire process ended.
Lane 3's execution lifetime was shorter than the run, producing three
consecutive FAIL rounds. Observability patches made the timeout more visible
without changing the untestable all-or-nothing execution shape.

**Trigger:** a handoff specifies a script that will make more than 50
sequential network calls, or Lane 1 otherwise expects one full run may outlive
a single Lane 3 execution turn. Before Lane 2 writes any code, the handoff's
Implementation Spec must require all three:

1. **Incremental checkpointing and resume:** persist progress after bounded
   units of work, with enough identity/state to resume safely without
   repeating completed mutations. Evidence cannot exist only as a terminal
   side effect of a fully completed run.
2. **A bounded-work control:** expose a deterministic limit such as
   `--limit N` so Lane 2 and Lane 3 can execute a representative slice within
   one turn and prove checkpoint/resume behavior.
3. **A network-free report mode:** read checkpoints/results locally and emit
   completion, remaining-work, and error evidence without making network
   calls or resuming the job.

Lane 1 includes a concrete test case for each capability. A qualifying
handoff that omits any of them is incomplete and must not be posted for
implementation. These requirements define a verifiable job shape; adding
progress logs, longer timeouts, or a terminal-only report does not satisfy
them.

## Live-Verification Specs for Pre-Existing Services — Cite a Duration Budget, Scope the Check

Real incident, HRSE2 #455: a pure structural refactor (no behavior change) required a Lane 3 test case that invoked a pre-existing production service (`run_background_sync`) live and compared before/after results. That service takes 78-98 seconds in real production runs (visible in the service's own logs the whole time) -- three times longer than a Lane 3 execution turn, every time. Three consecutive FAIL rounds chased a capture-mechanism problem (stdout buffering, then a file-write timeout) before recognizing the operation itself could never finish inside the verification window. A companion problem: the same test case demanded value-equality between two live runs against live external data (Gmail/Calendar), which is unfalsifiable on its own since real values legitimately differ run to run.

This differs from "Long-Running Script Handoffs" above: that section governs new scripts Lane 2 is about to write, where Lane 1 can require checkpoint/resume/bounded-work up front. Here the long-running thing is pre-existing code that cannot be redesigned by the issue under test.

**Before approving any Lane 3 test case requiring live invocation of a pre-existing service, Lane 1 must:**

1. **Cite a measured duration budget.** Check the service's own logs or prior run history for its actual wall-clock duration -- do not guess or assume it will finish inside a Lane 3 turn.
2. **If that duration exceeds a Lane 3 turn, scope the check to a bounded subset** -- a single entity, a single integration/branch of the service, or a direct call to an internal sub-function -- rather than a full, unbounded invocation.
3. **Assert return shape, not value-equality, whenever the subject depends on live external data.** Two runs against a live third-party dataset will legitimately differ; require key-set/structure comparison instead.
4. **Route any genuinely unbounded full-service run to the operator or a detached background job as a non-blocking deferred check.** It must never gate a PR inside a single Lane 3 turn.

## Plan-First Implementation — Reviewing Lane 2's Plan Before Credits Burn

`pitch-inspection` (above) reviews Lane 1's *design* before handoff. But
some handoffs deliberately delegate a design decision to Lane 2 — and Lane
2's resolution of that decision is new, unreviewed design content that
otherwise gets its first review only after implementation credits are
spent. Real incident, HRSE2 #233: a solo Lane 1 read of Lane 2's
unprompted plan caught two real gaps but still approved a design that
went on to generate the issue's recurring bug class across ~10 rounds —
the pause was right, the review was too shallow. This section makes the
pause required for a narrow, self-declared class and routes the review
through fresh context instead of a Lane 1 solo read. Full evaluation,
including the honest cost/benefit case against building this at all:
`docs/decisions/ADR-004-plan-first-implementation-and-comment-formatting.md`.

**Second real incident, ADR-005:** on HRSE2 #236, a handoff carried an
explicit, bolded operator-override instruction to plan-first — restated
three times in the same comment — and Lane 2 still implemented straight to
`main` before any plan was posted. `sticky-wicket`'s live diagnosis: this
was not a wording failure (the instruction was already maximally explicit)
but a co-delivery failure — the same comment that said "stop before
writing code" also contained a complete, numbered Implementation Spec,
i.e. everything needed to skip the gate was handed over *with* the gate.
Two structural fixes below (handoff-splitting, distinct relay triggers)
close that gap; see the ADR for the full incident record and the
rejected alternatives (a stronger single-comment wording, a git-hook-only
fix considered but not sufficient alone since it stops the *commit*, not
the premature write-then-discard cycle a hook can't distinguish from
legitimate iteration).

**Trigger — self-declared by the handoff, not a size judgment.**
`templates/lane1-handoff.md` carries a mandatory field, *Delegated
Judgment Calls* (design decisions Lane 1 explicitly leaves to Lane 2;
"none" is the common, zero-cost answer — most handoffs are unaffected).
Plan-first is required when any of:
1. Delegated Judgment Calls is anything other than "none."
2. The implementation's own operation mutates git state or live data (same
   condition as `pitch-inspection` trigger 3), whether or not the issue is
   under the Tooling Exception.
3. HITL explicitly says "Plan-first #N."

**Handoff-splitting — the gate is now physical, not voluntary.** For a
plan-first issue, Lane 1's handoff comment **omits the Implementation Spec
section entirely** — design content, affected-files table, root cause,
design alternatives, load-bearing assumptions, delegated judgment calls,
and test cases are all still posted, but the numbered step-by-step
instructions are withheld. There is nothing for Lane 2 to implement from
until the plan review passes, so "implement anyway" is no longer a
self-restraint failure — the spec simply isn't there yet.

**Process — one extra relay, never a full round-trip:**
- On "Plan #N" (see § HITL Gate Language step 2a) for a plan-first issue,
  Lane 2 posts its implementation plan as a comment on #N — covering, at
  minimum, its resolution of each delegated judgment call and the
  failure/cleanup paths of any git- or data-mutating mechanics — and
  **stops.** The plan is a natural prefix of work Lane 2 was doing anyway
  (it has already fetched the issue and read the cited files); the
  marginal cost is one comment.
- HITL relays "Plan up for #N" (→ Lane 1). Lane 1 invokes `pitch-inspection`
  in **plan-review mode** (see `agents/pitch-inspection.md`), passing the
  original handoff plus Lane 2's plan; the review covers only the delta
  Lane 2 introduced, not a re-review of the whole handoff. Lane 1 posts the
  verdict as a single comment on #N. This is a Claude-side subagent call —
  no gate cycle needed.
- **PROCEED / PROCEED WITH NAMED CHANGES:** Lane 1 posts the withheld
  Implementation Spec as a follow-up comment (incorporating any named
  changes), then HITL sends **"Implement #N"** — now unambiguous, since it
  is only ever sent after a verdict exists. Lane 2 implements from the
  newly-posted spec — no re-submission, no second review pass. **REFORGE:**
  the named flaw goes back to Lane 2 for one revised plan; if the second
  plan still draws REFORGE, that is an escalation to HITL, never a third
  review round. Same no-thrash cap as the Pre-Flight Second Read.
- A plan-first issue where Lane 2 starts implementing without a
  posted-and-reviewed plan is a protocol violation, same class as skipping
  the HITL close gate — file it per § STANDING-RULE VIOLATIONS GET FILED
  in `rules/universal-agent.md`, do not just note it in a gate comment.

**What this does not cover:** faithfulness of Lane 2's later completion
reports to the approved plan (verification-honesty, governed by
verify-live-not-source and Lane 1's full-diff review) and acceptance-
criteria defects in the issue itself (Lane 1/epic scope). HRSE2 #233's
false-completion-report rounds and its "passes twice consecutively"
contract error would not have been caught here — see the ADR for the full
honest partial-credit accounting.

## GitHub Comment Formatting — Every Lane, Every Post

Real incident, twice in one night: on HRSE2 #234 and #235, Lane 2's
completion comments arrived with swallowed/mangled code blocks — content
posted via an inline `gh issue comment --body "$(cat <<'EOF' ... EOF)"`
heredoc containing nested triple-backtick code fences collided with
GitHub's markdown parser and lost content, making the comment unreadable
without independently re-verifying every claim from scratch. This is a
mechanical formatting bug, not a judgment call — fix it as a standing rule,
not a subagent.

**Any lane posting a comment containing a code block:**
1. Write the comment body to a file first, then post via
   `gh api repos/OWNER/REPO/issues/N/comments -F body=@<path>` (REST — see
   harmonic-forge#220; PR comments use the equivalent `pulls` path) —
   never an inline heredoc with nested backticks.
2. **Self-check before considering the post done:** fetch the comment back
   (`gh api repos/OWNER/REPO/issues/comments/<id> --jq .body`, REST, or
   equivalent) and confirm it rendered legibly — no swallowed code blocks,
   no stripped content. This is the verify-live-not-source standard
   applied to a lane's own GitHub output, not just to code behavior.

## GitHub Account Scoping — Every Lane, Every Command

Core rule, incident record, and `gh-as` usage: see `universal-agent.md`'s
"GitHub account scoping — never `gh auth switch`" section (harmonic-forge#170
dedupe — this was a near-verbatim duplicate).

Residue not covered there: one-time setup is `gh-as --init <account>`;
`gh-as --list` shows each configured slot and the identity it actually
resolves to. **`gh-as` refuses to run** if a slot is unconfigured, its
token is expired, or the slot's authenticated identity doesn't match its
name — a command cannot silently execute against the wrong account
because a token was replaced out of band.

## Team Topology

| Role | Person | Responsibility |
|---|---|---|
| Platform owner | Marc | Owns `harmonic-forge`; sets golden paths; merges platform PRs |
| Feature delivery | Kyle (CymaGraph/HRSE2), Greg (Ke'nekted) | Consume golden paths; run the 3-lane loop locally; never edit platform rules directly |
| Product demand | Shawn | Defines acceptance criteria on issues; approves shipped features |

**Tool choice per lane is a per-collaborator decision, not a platform mandate.**
Kyle, Greg, Ajit, and future collaborators each pick whichever tool they
run for Lane 2/Lane 3 on their own machine — Claude Code and Codex are the
current known examples, with Gemini's qualification tracked by
harmonic-forge#317; not an exhaustive list. What's non-negotiable
regardless of tool choice is the protocol
itself (see the note under the lane diagram above): independent-eyes
gating, no lane closes/merges on its own, and — per the Lane 3 note above
— mechanical enforcement of the never-fixes-anything rule wherever the
chosen tool supports it, not prose alone.
