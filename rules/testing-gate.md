# Lane 3 Test Gate — Standard Path

Applies to backend and full-stack tickets. See `frontend-ui-golden-path.md`
for the UI-only variant.

| Metric | Threshold | Enforced by |
|---|---|---|
| Test pass rate | 100% | Devin AA — hard block on commit |
| Line coverage | ≥ 80% | Devin AA — hard block on commit |
| Test spec approval | HITL (Tech Lead) | Required before test execution begins |
| Max auto-fix retries | 3 | Escalates to Tech Lead on failure |

## Rules

<!-- R-0123 -->
1. Lane 3 receives the GitHub issue spec independently and writes its test
   spec from the issue's acceptance criteria — **it must not read Lane 2's
   implementation first.** Reading the code before writing tests produces
   tests that describe what the code does, not what the ticket required
   (test collusion). The issue is the oracle.
<!-- /R-0123 -->
<!-- R-0124 -->
2. The test spec goes to the Tech Lead for HITL approval (see
   `templates/hitl-test-review.md`) before any test executes.
<!-- /R-0124 -->
<!-- R-0125 -->
3. **Immediately after HITL approval, before Lane 3's first execution
   attempt, Lane 1 sweeps every test case in the approved spec for
   environment/fixture readiness** — what each TC actually needs to run
   (disposable containers, live local services, worktree-local config
   files, installed dependencies/browser binaries, etc.) — and provisions
   or verifies all of it in one pass. This is a Lane 1 responsibility, not
   Lane 3's: Lane 3 is generally barred from building/starting fixtures
   itself (see rule 8 and the state-changing-execution boundary in
   individual test specs). Skipping this step means each missing
   prerequisite is discovered reactively, one gate attempt at a time — the
   incident that prompted this rule (HRSE2 hrse#330, harmonic-forge#80,
   2026-07-21) cycled through branch-checkout, container-networking,
   missing-venv/.env/Chromium, and missing-fixture blocks across five
   separate rounds before this sweep step existed. Do the sweep once, up
   front, not as a retry loop.
<!-- /R-0125 -->
<!-- R-0126 -->
   - **The sweep also covers structural preconditions on the code itself,
     not just fixtures/environment** — does the implementation's own
     trigger condition, branch/merge state, or deployment position mean
     Lane 3 *cannot* observe a real live execution yet, independent of
     whatever fixtures are ready? Concretely: a CI job gated on
     `if: push to main` cannot be live-tested from an unmerged branch; a
     feature behind a flag/config that isn't set anywhere yet cannot be
     live-tested until it is. This class was missed even after rule 3
     existed (harmonic-forge#82, 2026-07-21 — Lane 1 swept fixtures/
     secrets but not the workflow's own `push`-to-`main`-only trigger
     condition, caught only because the operator asked "are you sure?" a
     second time). State explicitly in the sweep write-up whether each TC
     is executable as-is, or names the specific human-attended action
     (merge, deploy, flag flip) still required before it can run — don't
     let "the fixture is ready" stand in for "the test can actually run."
<!-- /R-0126 -->
<!-- R-0127 -->
   - **Post the sweep as a structured checklist in the handoff/gate
     comment** (one line per TC: executable-as-is, or the named blocking
     action), not a prose paragraph — matches the evidence-not-prose
     standard in rule 4 below, and see `3-lane-protocol.md`
     § Pre-Handoff Precondition Trace for the same discipline applied
     before Lane 2's implementation, not just before Lane 3's gate.
<!-- /R-0127 -->
<!-- R-0128 -->
   - **Mechanically required format (hrse#389, added after this rule
     failed to hold as prose twice — hrse#387, harmonic-forge#82):** the
     comment's heading must be exactly `## Gate-readiness sweep —
     <PREFIX><N>` (e.g. `## Gate-readiness sweep — H164`). **Lane 3 —
     either tool — must verify this comment exists on the issue thread,
     posted after the test-spec HITL-approval comment, before executing
     any TC. If absent: STOP immediately, execute nothing, report
     BLOCKED naming the gap.** This shifts the check from "trust Lane 1
     remembered" to "Lane 3 verifies before starting" — see each project's
     Lane 3 skill/directive file (e.g. HRSE2's
     `.devin/skills/lane3-gate/SKILL.md` and `AGENTS.md`) for the
     identical rule stated at the actual entry point.
     **The sweep leads with `Write tier <R|W|P>` — exactly one literal
     letter, the ceiling of every TC's tier. Prose ("mixed", "read-only")
     matches nothing and BLOCKs identically to omitting the line.**
     `check_lane3_ready.py` validates the `l1-post` footer, never the
     heading: confirm the byte-exact heading independently — "ready" from
     that tool is necessary, not sufficient.
<!-- /R-0128 -->
<!-- R-0335 -->
   - **The sweep leads with its own summary block, above any evidence
     (harmonic-forge#472).** Three bold-labelled lines directly under the
     heading, before the first `###` section or `<details>` block:

     ```markdown
     ## Gate-readiness sweep — H164

     **Readiness:** all 9 cases executable as written.
     **Blockers:** none.
     **Next:** Lane 3 executes, then reports PASS/FAIL.

     Write tier: R

     ### Per-case readiness
     1. TC1 — ready: dry-run mode live-verifiable, no writes.
     ```

     Readiness/blockers/next, **not** verdict/finding/next: a sweep is a
     PRE-execution artifact, so it has no verdict and rule 3's own
     fabricated-outcome check refuses one. `l1_post.py`'s `validate_lead`
     enforces this and refuses a sweep that buries its assessment — this
     paragraph documents an enforced rule rather than asking a lane to
     remember one.
<!-- /R-0335 -->
<!-- R-0336 -->
   - **The AE leads with what it authorized and what happens next.** Two
     lines, same position, same enforcement:

     ```markdown
     ## AE — H164

     **Authorized:** the spec in issuecomment-5548514543, against
     `feat/164-thing` @ `d5339ca`.
     **Next:** Lane 3 executes the approved cases and reports.
     ```

     An AE grants permission; it reports no finding, and it is not asked
     for one. Forcing it into a verdict/finding template produces a heading
     that lies.
<!-- /R-0336 -->
<!-- R-0129 -->
   - **A dependency present only via an undocumented ad-hoc install (not
     declared in the project's actual manifest — `requirements.txt`,
     `package.json`, etc.) is itself a sweep finding**, not something to
     silently install and move past — it means every *other* environment
     (a fresh Lane 3 worktree, a rebuilt container) is one dependency
     short of working (hrse#325: `pytest` ran in the main dev venv for
     months, never declared anywhere, so the first fresh gate venv to
     need it failed cold).
<!-- /R-0129 -->
<!-- R-0130 -->
4. After approval, Lane 3 executes tests against Lane 2's implementation.
   Live execution only — an "Evidence type: Source" citation (Lane 3 read
   the code and reasoned it should pass) does not satisfy this gate. Every
   check needs an actual run: a request/response, a log line, a before/after
   count.
<!-- /R-0130 -->
<!-- R-0131 -->
   - **Each check's gate report carries the evidence artifact itself, not
     just a prose claim that live execution happened** — the pasted
     command+output for an API/DB check, the log excerpt with timestamp, a
     screenshot/short recording for a UI check, the actual query output for
     a before/after count. A claim without its artifact does not satisfy
     this rule any more than a source-code citation does.
<!-- /R-0131 -->
<!-- R-0132 -->
   - Artifacts too large to paste into the issue comment go to
     `~/Harmonic_Projects/testplan/{issue}/`, with the gate comment linking
     the filenames — the same directory Lane 3 already uses for test
     plans/results, now also the artifact home.
<!-- /R-0132 -->
<!-- R-0133 -->
   - **When a TC's acceptance criterion is that a data-modifying write
     path actually *ran*, the pasted artifact must include the concrete
     before/after counts** — for hrse#849 that was "217/219 candidates
     resolved and written (106 inbound, 111 outbound), 2 genuinely
     indeterminate". This is a specialization of the artifact
     requirement above, not a new obligation: for a migration, "the
     actual query output for a before/after count" *is* the evidence.
     Once posted, apply the `migration-executed` label — that label, not
     the gate report, is what permits the issue to close (hrse#859).
     The gate report attests that test cases executed, which is a
     different claim from rows having changed.
<!-- /R-0133 -->
   - This exists so Lane 1's HITL step-5 review ("confirm every claim is
     backed by live execution") means inspecting an attached artifact, not
     trusting prose that live execution happened — the same
     verify-live-not-source discipline applied one level up, to the gate
     report itself.
<!-- R-0134 -->
   - **A designated, disposable test-identity's own auth token is
     sanctioned live-execution proof for a TC whose acceptance criterion
     is backend behavior, not the login flow itself** — Lane 3 does not
     need a spec amendment or fresh HITL approval to use one (HRSE2
     hrse#603, after Lane 3 correctly declined to improvise a bypass of
     the sanctioned auth flow on hrse#598 absent explicit spec cover).
     Concretely: if a project provisions a fixture like a designated
     `*_TEST_USER`/`*_TEST_PASSWORD` pair and a script that trades it for
     a bearer token (see HRSE2's `scripts/get_test_token.py` and its
     `.windsurfrules` pointer for a worked instance), a TC that asserts
     "endpoint X behaves correctly given a valid authenticated caller" is
     satisfied by that token directly — no separate proof that the token
     came from the real login/PKCE/SSO UI flow is required, because the
     application code under test cannot tell the difference and doesn't
     claim to care. This does **not** extend to any TC whose acceptance
     criterion *is* the login/auth flow itself (a browser SSO/PKCE/OAuth
     UI test, or anything verifying the identity-provider round trip) —
     those still require the real flow, unaffected by this carve-out. The
     credential side stays narrow too: only a credential the project has
     explicitly designated disposable/test-only qualifies — never a real
     user's credential, never anything resembling a production identity.
<!-- /R-0134 -->
<!-- R-0135 -->
5. If tests fail, Lane 3 may attempt up to 3 auto-fixes **of its own test
   spec/fixtures** (a bad assertion, a stale fixture, a wrong selector) — 
   **never of the application code under test.** A 4th consecutive failure
   on the same root cause escalates to the Tech Lead rather than retrying
   further. If the failure traces to a genuine bug in the implementation
   (not the test), that is not an auto-fix case at all — stop and report it
   as a gate finding, same as rule 6's style-pass violations, regardless of
   how trivial or obviously-correct the fix would be. See
   `3-lane-protocol.md`'s Lane 3 section for the full rule and the incident
   that prompted this clarification (HRSE2 #176).
<!-- /R-0135 -->
<!-- R-0136 -->
6. Once tests pass, Lane 3 performs a style/refactor pass per the project's
   `.windsurfrules`, then is unblocked to commit. **This pass is
   report-only — Lane 3 identifies violations, it does not fix them,
   whether the violation is pre-existing or was introduced by the change
   just gated.** Implementing a fix (even a small one, even one that only
   compresses lines to get back under a cap) is Lane 2's job. If the
   gated change itself introduced a violation (e.g. pushed a file over the
   line cap), Lane 3 reports it as a gate finding for the Tech Lead to
   route back to Lane 1/Lane 2 — it does not edit the file itself to
   clear its own finding. See `rules/universal-agent.md`'s no-ad-hoc-fixes
   rule — this applies to Lane 3 exactly as it does to Lane 1.
<!-- /R-0136 -->
<!-- R-0137 -->
7. If any check truly cannot be live-verified in the current environment
   (e.g. no browser available for a UI check), Lane 3 must say so explicitly
   rather than substituting a source-code citation — a partial, honest result
   is acceptable; a disguised one is not.
<!-- /R-0137 -->
<!-- R-0138 -->
8. **Fast-fail on external blockers.** If a live check is blocked by a
   genuine external dependency — a bug in another open issue this ticket's
   verification requires, a missing precondition, an environment gap that
   isn't this ticket's to fix — Lane 3 confirms the blocker is real with the
   minimum evidence needed (one clean repro, not exploratory workarounds),
   then stops and reports it as a gate finding **immediately**, not after
   attempting workarounds, mocks, or alternate verification paths to route
   around it. This is distinct from rule 5 (Lane 3's own test-spec issues) —
   an external blocker in another lane's work is never something to route
   around, mock past, or retry; it is always an immediate stop-and-report.
   See `3-lane-protocol.md`'s Lane 3 section for the full rule and the
   incident that prompted it (HRSE2 #204).
<!-- /R-0138 -->
<!-- R-0139 -->
9. **Provision a live preview before asking for browser evidence on an
   unmerged change — don't wait for the operator to notice nothing's
   there.** A UI-visual acceptance criterion (color/theme, layout,
   favicon/icon rendering, anything a jsdom/build check can't cover) needs
   a real browser pointed at the actual change — but an implementation
   sitting on Lane 2's local branch, pre-merge, isn't reachable by the
   operator's normal browser tab at all. Real incident (HRSE2 #400,
   2026-07-27): Lane 3 correctly reported TC4-6 BLOCKED pending browser
   evidence; the operator checked the live app, saw nothing (because the
   change wasn't merged), and had to ask for a preview to be set up as a
   separate step. On a project with per-worktree port isolation (HRSE2
   #341's `.mise.local.toml` mechanism, or equivalent), the handoff for
   any issue with a real-browser TC should include, up front: the exact
   preview URL (isolated port, not the operator's main dev port) and the
   exact command to bring it up (e.g. `cd <lane-worktree> && mise run
   restart --no-bump --no-git`), so the operator can open one tab and
   look, not diagnose why the main app looks unchanged. This is a Lane 1
   handoff-authoring responsibility, not something Lane 2/3 improvise
   after the operator asks.
<!-- /R-0139 -->
<!-- R-0140 -->
   - **Capture the evidence yourself when you can.** If the
     Claude-in-Chrome browser tools are available to the session, Lane 1
     takes the screenshot rather than deferring every operator-environment
     TC to the human. This sits inside Lane 1's smoke-test-level
     live-verification scope, not in tension with it: the evidence is
     something Lane 3 is structurally incapable of producing (it has no
     browser), not a re-check of something Lane 3 already covered.
<!-- /R-0140 -->
   - **Tab-group isolation.** The extension drives only tabs in its own
     MCP-managed tab group — not the operator's regular open tabs, even in
     the same Chrome profile. To reach an already-authenticated session,
     don't try to attach to the operator's tab; navigate your own MCP tab
     to the same URL. Cookies are shared per-origin within a profile
     regardless of which tab opened them.
   - **Per-worktree ports don't retarget a frontend's API base URL.**
     Port-isolation mechanisms control what each service *binds to*; they
     do not rewrite a client-side absolute API origin. A `.env`/
     `.env.example` pinning something like
     `VITE_API_BASE_URL=http://localhost:8002/api` silently defeats the
     isolation — every lane's frontend keeps calling the *main* checkout's
     backend, surfacing as intermittent `Failed to fetch`/503s that read as
     "the backend isn't up" when it is, just on another port. Setting that
     absolute override to a cross-port value instead introduces a second
     silent failure (CORS-blocked cross-origin `fetch`). In a Vite-proxy
     setup the fix is to leave the absolute override unset so the client
     falls back to same-origin relative requests, and let the proxy's own
     backend-target variable do the routing. Rule out this class of
     misconfiguration before concluding a worktree backend is down.
     (Still live in HRSE2 as of 2026-08-12: `frontend/.env.example` pins
     port 8002.)

## A green local gate is necessary and NOT sufficient — CI is the second required signal (harmonic-forge#504)

<!-- R-0354 -->
**A Lane 3 PASS may not be posted while the PR's own required checks are
failing, pending, or absent.** The local gate and CI are two signals and the
merge has to survive both; only CI runs in the environment the merge actually
lands in. A PASS must state the head SHA it gated and the CI conclusion
observed for that SHA, so a reader can tell what was checked rather than
trusting the verdict. Checks that have not completed — including a commit with
**no** check runs at all — make the correct verdict `BLOCKED`, not `PASS`:
waiting is the behaviour, not a judgment call. Enforced by
`tools/gh/gate_ci.py`, called from `post_lane_discussion.py` for any body that
IS a gate report — recognised by its `## Lane 3 Gate Results` heading, exactly
as `lane_state.py` recognises one, and NOT by the `--kind` the author passed.
FAIL and BLOCKED are never gated, because a check that can silence a failure
report is worse than none.

**Coverage is one path of several, and saying so is part of the rule.** Lane 3
has no raw-post restriction by design, so `gh issue comment` and
`tools/gh/post_comment.py` remain open routes that this check does not see.
Both most-recent real gate results went through the covered path, so the
enforcement point is the live one — but a rule claiming universal enforcement
it does not have is the same class of error as a gate claiming a green it did
not read.
<!-- /R-0354 -->

### Why this is a check and not a line of guidance

2026-09-07, measured: a gate returned **PASS at 00:12**, CI reported
**failure at 00:13 on the same code**, and `main` stayed red for three hours
across two merges with no lane noticing.

The three failures read the operator's live `~/.claude/settings.json`. On this
machine the key exists and the assertions hold; on a runner it does not and
they fail. **Lane 3 runs on the operator's machine**, so the gate is
*structurally* incapable of catching that class — and that class is exactly
what gets written when the feature under test IS operator-local state, as it
was. Lane 3 executed its spec correctly and the spec passed. The gate's
environment is simply not the environment the merge has to survive.

So this rule could not be a sentence saying "also look at CI." This repo's own
repeated finding is that prose compliance degrades under context pressure, and
a long gate run is that pressure. #504's AC1 says it outright: *a prose
instruction alone does not satisfy this.*

### The general shape, so it is recognisable elsewhere

**A verification asserts against an endpoint, and the endpoint that matters is
the last one, not the first one convenient to read.**

An independent witness, one week and one domain away: a QA brief describing an
AI-generated suite passing 14 of 14 while the application charged the wrong
amount, because the prompt asserted what the screen *displayed* and never what
was *charged*. The fix was one line — *follow the money all the way through.*
Structurally identical to `00:12 PASS -> 00:13 CI failure`: the gate asserted
the first green thing it could read.

The corollary that catches the next instance: **"no signal yet" is not "no
problem."** An empty check list is not a green one. That mistake was made
again while implementing this very issue — a wait-loop tested "every check has
completed", an empty list satisfied it vacuously, and it reported done on a PR
whose CI had not yet registered.

### AC6, answered rather than assumed: does a green PR check guarantee a green `main`?

**No, and the rule above is deliberately scoped to the PR's checks anyway.**

A PR's checks run against a *merge preview* — the head merged into the base at
that moment. Two things break the guarantee: the base can move between the
check and the merge (semantic conflict, where both sides are individually green
and their combination is not), and a squash merge produces a commit that no CI
run has ever seen.

That is a real gap and it is NOT closed here, because closing it means gating
on a signal that does not exist until after the merge — which cannot be a
precondition of merging. What closes it is noticing afterward, which is a
different mechanism (watching `main`'s own CI and reacting), and a different
issue. Recorded here so the next reader knows the scope is a choice rather than
an oversight: this rule removes the case that actually bit, where the PR's own
checks were red and nothing looked.
