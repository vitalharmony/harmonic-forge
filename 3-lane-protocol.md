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
Devin AA is the reference pairing this doc was originally written against
for Lane 2/Lane 3 and stays the default example throughout, but Codex
(assignable to either Lane 2 or Lane 3 per issue — see the operator's own
project rules file, e.g. `.windsurfrules`, for the exact trigger phrasing)
is a second live example. Claude Code is Lane 1's reference/default
implementation — this platform's own authoring tool — but is not fixed by
name; an operator may assign a different tool to Lane 1 via that tool's
own equivalent mechanism (see § Lane 1 below). Different collaborators
(see Team Topology below) will likely bring their own tool preferences
for these roles — the protocol's actual requirements (independent-eyes
gating, no lane closes/merges on its own, HITL-gate language, etc.) are
tool-agnostic and apply identically regardless of which specific tool is
filling any lane on a given session.

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
- Does not write production code directly (exception: explicit platform/
  tooling assignments — see `rules/universal-lane1.md`).

## Lane 2 — Muscle (reference tool: Devin Local)

*Devin Local is the reference implementation this section is written
against — an operator assigning Codex or another tool to Lane 2 follows
the same requirements below, with that tool's own equivalent mechanism
where a Devin-specific file/path is named. See the project's own rules
file (e.g. HRSE2's `.windsurfrules` "Codex role" section) for how a given
tool's Lane 2 assignment is actually triggered and scoped.*

- Given a short trigger (e.g. "implement #N") rather than pasted content,
  **fetches the issue and Lane 1's handoff comment from GitHub directly**
  (`gh issue view N --comments`) before doing anything else — same
  expectation already placed on Lane 3 for its own independent issue read.
  Do not wait for or require the handoff to be pasted in chat.
- Executes exactly what the handoff specifies — no scope creep.
- Stays within the file targets the handoff defines.
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
  apply in the moment.
- Reads the cited file(s) and quotes the root-cause line before editing.
- Obeys `rules/universal-agent.md` + language-specific rule files (300-line
  cap, no raw LLM/API calls outside the designated gateway, parameterized
  queries).
- Signals completion with a structured diff summary back to Lane 1.

## Lane 3 — Control Gate (reference tool: Devin AA)

*Devin AA is the reference implementation this section is written
against — an operator assigning Codex or another tool to Lane 3 must
still meet every requirement below (independent read before gating, no
edits/fixes/commits, FAIL-and-report rather than work around a blocker,
no close/merge without explicit operator instruction), via that tool's
own equivalent mechanism where a Devin-specific file/path is named below.
**Mechanical enforcement, not prose alone, is what actually holds** — this
project's own history (see `.devin/skills/lane3-gate/SKILL.md`'s origin)
is a hard-tool-restriction gate built specifically because prose-only
Lane 3 rules were violated under pressure more than once. Any tool newly
assigned to Lane 3 should get an equivalent hard restriction (a
permission/sandbox profile denying write/edit/commit/push tools for that
session) wherever the tool supports one, not just an instruction file it's
trusted to follow.*

- **Own directives file: `~/.config/devin/AGENTS.md`** (local, machine-specific
  — not part of this repo, not synced by `sync_rules.py`). This is where
  direct, durable corrections to Lane 3's own behavior go, separate from the
  shared protocol/rule files above that steer all three lanes. Devin AA also
  writes to this file itself (not purely human-maintained) — check it, don't
  assume the `harmonic-forge` rules are the only lever when Lane 3's behavior
  needs adjusting.
- **Own skill file, per-project: `{project}/.devin/skills/lane3-gate/SKILL.md`**
  (repo-local, not `~/.config` like `AGENTS.md` above, and — as of 2026-07-10
  — not yet synced across projects by `sync_rules.py`; currently HRSE2-only).
  This is a hard-wired enforcement mechanism for the never-fixes-anything
  constraint two sections below, drafted by Devin AA itself after diagnosing
  that prose rules alone weren't preventing it from rationalizing past them
  ("I found the fix and it was easy" / "the fix is obviously correct"). The
  skill explicitly names `harmonic-forge/3-lane-protocol.md` and
  `rules/testing-gate.md` as authoritative — it is the enforcement layer, not
  a second source of truth, and should be updated to match if the two ever
  drift. Cites the real incidents (`#176`, `#52`) that produced it. If this
  pattern proves out, worth revisiting whether it should be added to
  `sync_rules.py`'s distribution so other projects get the same hard-wired
  protection, not just prose.
- Runs locally. Reads the GitHub issue independently — **never reads Lane
  2's code before writing its test spec.** This now explicitly includes
  **Lane 2's own implementation-report comment on the issue thread** — Lane
  1's handoff and Lane 2's diff summary both post to the same issue (see
  Lane 1/Lane 2 sections above), so when deriving its test spec Lane 3
  reads only: the issue body, Lane 1's original handoff comment, and (if
  present) a second Lane 1 comment specifically addressed to Lane 3 —
  written after Lane 2 finishes, carrying any caveats the HITL gate turned
  up (e.g. "the named test-pair's data has since changed, construct a
  fixture instead" — a real example from #153). Lane 3 must not open or be
  influenced by any Lane 2 comment already on the thread until after its
  spec is written and approved. Co-locating every lane's output on one
  issue is a deliberate convenience (it's what lets Lane 1 review by
  reading the thread instead of relaying pasted text) — it must not become
  a backdoor that lets Lane 3 anchor on Lane 2's self-report.
- Writes a test spec from the issue's acceptance criteria.
- Submits the test spec for Tech Lead HITL approval
  (`templates/hitl-test-review.md`) before executing anything.
- After approval, executes tests against Lane 2's implementation. See
  `rules/testing-gate.md` (standard) or `rules/frontend-ui-golden-path.md`
  (UI-only variant) for thresholds.
- **Interactive tool modes are never used for automated execution.** Any
  browser-automation invocation (Playwright, etc.) runs headless with a
  non-interactive reporter (e.g. `--reporter=line`) — never `--debug`,
  `--ui`, `codegen`, or any other mode that opens an inspector/GUI and
  blocks waiting for human interaction. A collapsed or non-interactive
  terminal has no way to surface that prompt, so the command just hangs
  until a human notices and manually cancels it — the same failure shape
  as the `sudo`/`npx install` hangs already seen this session, just in a
  different tool. If a command needs interactive confirmation, that's a
  signal to stop and ask, not to keep invoking variants of the same
  command hoping one works headlessly.
- **Lane 3 is the only lane authorized to execute a data-modifying
  script's write/apply path.** When an issue's scope is itself a data
  migration or correction (not just testing already-written application
  code — e.g. #155's shape), the test spec submitted for HITL approval
  must explicitly say so: the plan includes actually running the
  migration for real, not just verifying it works against fixtures. HITL
  approval of that spec is the approval to execute it. This is a
  deliberate reuse of the pre-execution approval gate Lane 3 already has
  for every test spec — it's a more suited fit for this kind of
  goal-seeking, spec-then-execute action than Lane 2's mechanical
  implement-exactly-what's-specified role, and it closes the #154/#155
  gap structurally: nothing runs against real data without HITL having
  explicitly approved that specific action first.
- Blocked from committing until 100% of tests pass at the required coverage
  threshold.
- **Lane 3 never fixes application code, ever, under any circumstance —
  full stop.** This applies regardless of whether the bug is self-introduced,
  pre-existing, trivial, or blocking test execution entirely. Lane 3's only
  permitted write actions are: writing/refactoring its own test specs and
  test scripts, and executing an already-HITL-approved data-migration
  script's write/apply path (the one explicit exception above — a
  pre-approved action, not an in-the-moment fix). Every other case: stop
  and report back to Lane 1, or ask the human operator directly for
  authorization to proceed (as HITL did live during #176's port-conflict
  fix — that's the correct pattern: human directs it live, not Lane 3
  deciding on its own). "3 auto-fix attempts max" below refers strictly to
  retrying/adjusting Lane 3's own test spec against a genuine test-authoring
  problem (a bad fixture, a wrong assertion) — never to modifying the code
  under test. Real incident: on HRSE2 #176, Lane 3 self-fixed three things
  during one gate run — one was live-authorized by the human operator (fine),
  two were not (a real pre-existing bug fixed inline instead of reported,
  and a hardcoded plaintext credential written into a compose file to work
  around a variable-interpolation problem instead of fixing the invocation
  or reporting it). Marc: "L3 is falling into 'git 'er done' bias mode more
  than L1 or L2 and it shouldn't be fixing ANYTHING EVER, only running
  tests, scripts, or refactoring." **Why this matters structurally, not just
  as a boundary rule**: Marc: "we WANT the tests to fail if L1/L2 built
  something wrong, we don't want it intervening and fixing it, defeats the
  purpose of the protocol." Lane 3's entire value is as an *independent*
  check — a test that gets silently patched by the tester the moment it
  fails can never fail meaningfully again, which makes the whole gate
  structure pointless. A failing test is the protocol working correctly,
  not a problem for Lane 3 to make go away.
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
  isn't this ticket's to fix — Lane 3 confirms the blocker is real with the
  minimum evidence needed (one clean repro, not exploratory workarounds),
  then stops and reports it as a gate finding **immediately**. It does not
  attempt workarounds, mocks, or alternate verification paths to route
  around it first. Real incident: on HRSE2 #204, Lane 3's live delete-flow
  test depended on a mutation endpoint broken by a concurrent bug in #187;
  rather than reporting this immediately, Lane 3 tried several workarounds
  (including a mocked-browser test) before finally surfacing the blocker —
  burning real cost chasing a blocker that had *already been fixed* by the
  time the report was filed. Marc: "L3 burned 3% of my credits trying all
  kinds of crazy work-arounds instead of admitting it was blocked. This is
  not acceptable. It needs to follow the fast-fail principle which is
  doctrine in software development." This is distinct from the auto-fix
  rule above (which governs Lane 3's own test-spec issues, not external
  blockers) — an external blocker in another lane's work is never something
  to route around, mock past, or retry; it is always an immediate
  stop-and-report.

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
a long batched or unattended run isn't suspended mid-work; and, **for
Claude-family CLIs only** (`LANE_CLI` unset or starting with `claude` —
confirmed live, `tools/lane/lane3` line 43, harmonic-forge#179), defaults
to `--permission-mode auto` (override with `LANE_PERMISSION_MODE`, or
pass `--permission-mode` explicitly). A non-Claude `LANE_CLI` (e.g.
`codex`) never receives this flag injection — it broke Codex's own CLI
argument parsing before harmonic-forge#179 qualified the condition.
**Proper hygiene is restarting the
session with the right script, never redirecting a running session into
a different lane role mid-conversation** — `LANE` is fixed for a
process's entire lifetime by design (see below), so there is nothing a
running session could do to change it even if asked to.

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

Worktree isolation (above) prevents *cross-lane* directory collisions, but
each lane's own worktree can still end up dirty at a bad moment (e.g. Lane
1 editing docs in the main checkout while making an unrelated commit). The
following discipline still applies **within each lane's own directory**,
by **every** lane, not just Lane 3:

**Never yield control — post a completion comment, hand off, stop, or
otherwise signal "done" — while leaving uncommitted changes in the shared
directory.** Every stopping point ends in a clean `git status`. This
applies equally to:

- **Lane 1** (Claude Code) — including doc-only commits (sprint-plan
  reconciliation, rules edits); never run a broad `git add` without
  reviewing the actual file list first, and never start editing without
  confirming the working tree is clean and it's your own change producing
  the diff.
- **Lane 2** (Devin Local) — a "no push requested yet" completion report
  must still mean the branch itself is fully committed; an in-progress
  follow-up fix (even a small one, even one Lane 1 or the operator asked
  for) gets its own commit before the session ends, not left staged or
  unstaged for someone else to find later.
- **Lane 3** (Devin AA) — beyond the existing never-fixes-anything rule
  (which already bars Lane 3 from *creating* uncommitted changes), Lane 3
  must not assume an uncommitted diff it finds mid-gate belongs to its own
  run. If Lane 3 discovers uncommitted changes when starting a gate, that
  is itself a stop-and-report condition (same fast-fail principle as an
  external blocker) — not something to puzzle out solo, work around, or
  silently gate against.

**Real incidents this rule exists to prevent** (HRSE2, 2026-07-14/15,
same overnight session): a doc-only Lane 1 sprint-plan commit swept up
Lane 2's in-progress implementation files via a broad `git add` (#227);
Lane 3 found and had to stash an unrelated uncommitted file mid-gate on
#265's run rather than touching it (correct behavior, but only possible
because Lane 3 checked first); Lane 3's gate on #274 stalled on confusion
from Lane 2's own uncommitted follow-up fix sitting in the tree; and Lane
1's own attempt to fix a related tooling issue (#276) *while* Lane 3 was
actively mid-gate compounded the confusion further by stashing/branch-
switching in the same directory Lane 3 was reading from. Four variations
of the same root cause in one session — this is a pattern, not bad luck.

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

**Repo-prefixed issue numbers, when more than one repo is in flight.**
Once a collaborator works across multiple repos in the same session (e.g.
`vitalharmony/hrse` and `vitalharmony/harmonic-forge`), a bare `#N` is
ambiguous the moment both repos happen to have an open issue with the same
number — a real incident, HRSE2 2026-07-18: a status update named `#26`
with no prefix, and the two repos' issue `#26`s were entirely unrelated
work. Prefix the number with the repo: `H<N>` = `vitalharmony/hrse`#N,
`F<N>` = `vitalharmony/harmonic-forge`#N, `I<N>` =
`vitalharmony/cymagraph-infra`#N (added 2026-08-08 — cymagraph-infra-
specific issues, tenant provisioning, tofu/doctl wrappers, infra bugs,
file on that repo's own tracker rather than harmonic-forge; harmonic-forge
stays scoped to cross-project platform tooling). Applies to every trigger
word in this section — "Implement H26", "Test F26", "Plan-first F58",
"Reimplement H313", "Test I253" — the trigger vocabulary itself is
unchanged, only the issue reference is disambiguated. Note: unlike `hrse`
and `harmonic-forge`, `cymagraph-infra` has no dedicated Projects v2 board
and is not tracked by `docs/PRIORITIES.md`'s board-sync (as of this
writing) — `I<N>` issues are cross-referenced only from an `H`/`F` entry
that depends on one, not sequenced directly.

**Universal, not just for triggers (extended 2026-07-19).** Every issue
reference — a status update, a summary, a plain mention in a comment or
in chat, not only the trigger phrases above — carries the `H`/`F`/`I`
prefix, always, with no "obviously which repo from context" exception. HITL
often relays an agent's own issue references directly into commands sent
to other lanes; an unprefixed number forces HITL to disambiguate it
first, which defeats the convention's purpose. "Closed #313" is wrong;
"Closed H313" is right, in a summary just as much as in a trigger.

**Lane-status shorthand.** A quick status update, not a trigger — `L<N>`
+ one letter: `L2P` (Lane 2 posted its plan, plan-first issues only),
`L2D` (Lane 2 done/implementation posted), `L3P` (Lane 3 gate passed),
`L3F` (Lane 3 gate failed). This is a pointer to go read the lane's actual
report on the issue thread, never a substitute for it — the verification
standard above still applies in full. Combine with a repo-prefixed number
space-separated (`L3F H26`), not concatenated (`L3FH26`) — concatenating
collides visually whenever the result letter and repo letter are both
`F` (Fail + harmonic-**F**orge).

`AE` (approved, execute) — the operator's go-ahead for Lane 3 to run the
TCs in an already-approved test spec, distinct from approving the spec's
content (that's the `L3S` → HITL-approval step itself). Like every other
trigger phrase in this section, `AE` must be posted as an actual issue
comment, not only said to Lane 1 in chat — Lane 3 verifies it
independently on the thread before executing any TC, exactly as it
already verifies the gate-readiness sweep.

**Before acting on any trigger, the receiving lane checks the issue's
state.** If `#N` is already closed, stop and ask HITL to confirm the
number before doing anything — don't proceed on the assumption a closed
issue was meant. A typo'd issue number (fat-fingering a digit, or naming
one that was just closed a few messages ago) is an easy, low-cost mistake
to make in a fast-moving session with many issues open at once, and
proceeding against the wrong issue wastes a full lane cycle and produces
a confusing record on the wrong thread. This applies to whichever lane
receives the trigger — Lane 1, Lane 2, or Lane 3 alike.

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
4. **HITL says "Test #N"** (→ Lane 3). Lane 3 independently fetches the
   issue body, Lane 1's original handoff comment, and Lane 1's
   Lane-3-addressed comment from step 3 — never Lane 2's comment — derives
   a test spec, and submits it for HITL approval
   (`templates/hitl-test-review.md`) before executing anything. After
   approval, Lane 3 executes and posts its gate report as a comment on #N.
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
spent. Real incident: on HRSE2 #233, Lane 2 posted an implementation plan
unprompted and waited for Lane 1 sign-off; the solo Lane 1 read caught two
real gaps but approved the disposable-branch design that went on to
generate the issue's recurring bug class across ~10 rounds. The pause was
right; the review was too shallow. This section makes the pause required
for a narrow, self-declared class and routes the review through fresh
context instead of a Lane 1 solo read. Full evaluation, including the
honest cost/benefit case against building this at all:
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
  marginal Devin cost is one comment.
- HITL relays "Plan up for #N" (→ Lane 1). Lane 1 invokes `pitch-inspection`
  in **plan-review mode** (see `agents/pitch-inspection.md`), passing the
  original handoff plus Lane 2's plan; the review covers only the delta
  Lane 2 introduced, not a re-review of the whole handoff. Lane 1 posts the
  verdict as a single comment on #N. This is a Claude-side subagent call —
  no Devin credits, no gate cycle.
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
   `gh issue comment --body-file <path>` (or the PR/comment equivalent) —
   never an inline heredoc with nested backticks.
2. **Self-check before considering the post done:** fetch the comment back
   (`gh issue view N --json comments` or equivalent) and confirm it
   rendered legibly — no swallowed code blocks, no stripped content. This
   is the verify-live-not-source standard applied to a lane's own GitHub
   output, not just to code behavior.

## Team Topology

| Role | Person | Responsibility |
|---|---|---|
| Platform owner | Marc | Owns `harmonic-forge`; sets golden paths; merges platform PRs |
| Feature delivery | Kyle (CymaGraph/HRSE2), Greg (Ke'nekted) | Consume golden paths; run the 3-lane loop locally; never edit platform rules directly |
| Product demand | Shawn | Defines acceptance criteria on issues; approves shipped features |

**Tool choice per lane is a per-collaborator decision, not a platform mandate.**
Kyle, Greg, Ajit, and future collaborators each pick whichever tool they
run for Lane 2/Lane 3 on their own machine — Devin Local/Devin AA and
Codex are the two known examples as of this writing, not an exhaustive
list. What's non-negotiable regardless of tool choice is the protocol
itself (see the note under the lane diagram above): independent-eyes
gating, no lane closes/merges on its own, and — per the Lane 3 note above
— mechanical enforcement of the never-fixes-anything rule wherever the
chosen tool supports it, not prose alone.
