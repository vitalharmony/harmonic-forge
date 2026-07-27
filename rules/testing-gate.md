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

1. Lane 3 receives the GitHub issue spec independently and writes its test
   spec from the issue's acceptance criteria — **it must not read Lane 2's
   implementation first.** Reading the code before writing tests produces
   tests that describe what the code does, not what the ticket required
   (test collusion). The issue is the oracle.
2. The test spec goes to the Tech Lead for HITL approval (see
   `templates/hitl-test-review.md`) before any test executes.
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
   - **Post the sweep as a structured checklist in the handoff/gate
     comment** (one line per TC: executable-as-is, or the named blocking
     action), not a prose paragraph — matches the evidence-not-prose
     standard in rule 4 below, and see `3-lane-protocol.md`
     § Pre-Handoff Precondition Trace for the same discipline applied
     before Lane 2's implementation, not just before Lane 3's gate.
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
   - **A dependency present only via an undocumented ad-hoc install (not
     declared in the project's actual manifest — `requirements.txt`,
     `package.json`, etc.) is itself a sweep finding**, not something to
     silently install and move past — it means every *other* environment
     (a fresh Lane 3 worktree, a rebuilt container) is one dependency
     short of working (hrse#325: `pytest` ran in the main dev venv for
     months, never declared anywhere, so the first fresh gate venv to
     need it failed cold).
4. After approval, Lane 3 executes tests against Lane 2's implementation.
   Live execution only — an "Evidence type: Source" citation (Lane 3 read
   the code and reasoned it should pass) does not satisfy this gate. Every
   check needs an actual run: a request/response, a log line, a before/after
   count.
   - **Each check's gate report carries the evidence artifact itself, not
     just a prose claim that live execution happened** — the pasted
     command+output for an API/DB check, the log excerpt with timestamp, a
     screenshot/short recording for a UI check, the actual query output for
     a before/after count. A claim without its artifact does not satisfy
     this rule any more than a source-code citation does.
   - Artifacts too large to paste into the issue comment go to
     `~/Harmonic_Projects/testplan/{issue}/`, with the gate comment linking
     the filenames — the same directory Lane 3 already uses for test
     plans/results, now also the artifact home.
   - This exists so Lane 1's HITL step-5 review ("confirm every claim is
     backed by live execution") means inspecting an attached artifact, not
     trusting prose that live execution happened — the same
     verify-live-not-source discipline applied one level up, to the gate
     report itself.
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
7. If any check truly cannot be live-verified in the current environment
   (e.g. no browser available for a UI check), Lane 3 must say so explicitly
   rather than substituting a source-code citation — a partial, honest result
   is acceptable; a disguised one is not.
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
9. **Provision a live preview before asking for browser evidence on an
   unmerged change.** Lane 3 has no browser — operator-environment TCs on
   code that hasn't landed on `main` are structurally unreachable to it, not
   just inconvenient. Lane 1 provisions the preview (per-worktree port
   isolation, backend brought up, any local-only activation flags the
   TC needs), states the exact bring-up command and URL in the
   gate-readiness sweep (rule 3), and — when the Claude-in-Chrome browser
   extension is enabled for the session — captures the actual evidence
   itself rather than deferring every operator-environment TC to the
   human. This is within Lane 1's smoke-test-level live-verification
   scope, not a Lane-3-duplication concern, because it is evidence Lane 3
   is structurally incapable of producing, not a re-check of something
   Lane 3 already covered.
   - **Session mechanics:** the extension being visible/pinned in the
     browser toolbar does not mean its MCP tools are attached to the
     current Claude Code session — that attachment happens once at
     session start. Run `/chrome` to enable, then restart Claude Code;
     mid-session enabling alone will not surface the
     `mcp__claude-in-chrome__*` tools.
   - **Tab-group isolation:** the extension only sees/drives tabs in its
     own MCP-managed tab group, not the operator's regular open tabs,
     even in the same Chrome profile. To reach an operator's
     already-authenticated session, don't try to attach to their tab —
     navigate your own MCP tab to the identical URL; cookies are shared
     per-origin within the same browser profile regardless of which tab
     opened them.
   - **Worktree API-base-URL gotcha (found live, HRSE2 hrse#414):**
     per-worktree port isolation (`HRSE_BACKEND_PORT` etc., hrse#341)
     only controls what port each service *binds to* — it does not
     automatically retarget a frontend's client-side API base URL. A
     `.env`/`.env.example` (or an equivalent config file) that hardcodes
     an absolute backend origin (e.g. `VITE_API_BASE_URL=http://host:main-port/api`)
     silently defeats the isolation: every lane's frontend keeps calling
     the *main* checkout's backend instead of its own, surfacing as
     intermittent `Failed to fetch`/503s that look like "the backend
     isn't up" when it actually is — just on the wrong port. Two distinct
     variables can exist for two distinct purposes (a dev-proxy target
     server-side vs. an absolute client-side override); setting the
     absolute one to a cross-port value introduces a *second* failure
     mode (CORS-blocked cross-origin `fetch`, also silent). The correct
     fix in a Vite-proxy setup is to leave the absolute override unset so
     the client falls back to same-origin relative requests, and let the
     proxy's own backend-target variable do the actual port routing.
     Check for this class of misconfiguration before concluding a
     worktree backend is genuinely down.
