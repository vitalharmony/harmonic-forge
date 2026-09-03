# harmonic-forge Transaction Log

Auto-maintained by `mise run commit` (`scripts/git_commit.py` + `tools/transaction-log/`) — appends a delta summary in the same commit as the code change it describes (headline = verbatim commit message). Cleared on **push to main**, not a version bump — this repo has no running artifact to stamp, so push is its genuine "publish" event (see `mise.toml`'s header comment). Full history: `git log -p transaction-log.md`. Read this file at session start for recent context. Do not edit by hand.

<!-- TRANSACTION_LOG_START -->
## fix(hooks): preclose-inspection findings — python heredoc writes, /dev/null and quoted '>' false positives, harmonic-forge Edit|Write wiring (harmonic-forge#440)
- .claude/settings.json               |  5 +++
- tools/hooks/model_tier_gate.py      | 76 +++++++++++++++++++++++++++++++++++--
- tools/hooks/test_model_tier_gate.py | 42 +++++++++++++++++++-
- 3 files changed, 119 insertions(+), 4 deletions(-)

## fix(hooks): model_tier_gate covers Bash writes, exempts Lane 3 (harmonic-forge#440)
- .codex/hooks.json                   |   6 ++
- tools/hooks/model_tier_gate.py      | 114 +++++++++++++++++++++++-
- tools/hooks/test_model_tier_gate.py | 171 ++++++++++++++++++++++++++++++++++++
- 3 files changed, 290 insertions(+), 1 deletion(-)

## fix(hygiene): report open, mergeable, green PRs — the merge-forgotten state (harmonic-forge#438)

Nothing in the toolchain reported an open PR that is mergeable and passing
but not merged; `audit_repo()`'s own skip of open-PR branches (correct —
not stranded) meant that state was invisible everywhere, not mislabeled.
PRs #1451/#1501/#1513 sat unmerged, unnoticed, for up to a day.

- New `audit_open_prs()`: one detail fetch per currently-open PR (mergeable/
  mergeable_state only exist there, never on the list endpoint), split into
  `green_unmerged_prs` (mergeable and mergeable_state == 'clean' — fails the
  check) and `blocked_open_prs` (waiting on a human — report-only).
- Existing STRANDED/ORPHANED classification (audit_repo) untouched; the
  else-arm docstring stays as-is per the issue author's own correction.
- 9 new tests; all 765 tests pass.
- tools/gh/repo_hygiene.py      |  85 +++++++++++++++++++++++++++-
- tools/gh/test_repo_hygiene.py | 125 ++++++++++++++++++++++++++++++++++++++++++
- 2 files changed, 208 insertions(+), 2 deletions(-)

## feat(lane3): add dual fixed-root Gemini adapters
- tools/lane/lane3                                 | 18 -----
- tools/lane/policies/gemini-lane3.toml            | 68 +++++++++++++++++--
- tools/lane/test_lane3_context_mcp.py             | 10 +--
- 5 files changed, 166 insertions(+), 31 deletions(-)

## fix(lane3): choose Gemini target before launch
- tools/lane/lane3 | 18 ++++++++++++++++++
- 1 file changed, 18 insertions(+)

## feat(lane3): fetch named Gemini gate comments
- tools/gemini/lane3-context/lane3_context_mcp.py  | 28 ++++++++++++++++++++++++
- tools/lane/policies/gemini-lane3.toml            |  6 +++++
- tools/lane/test_lane3_context_mcp.py             | 13 +++++++++--
- 4 files changed, 46 insertions(+), 3 deletions(-)

## fix(lane3): route Gemini context by issue prefix
- tools/gemini/lane3-context/lane3_context_mcp.py | 63 +++++++++----------------
- tools/lane/test_lane3_context_mcp.py            | 49 ++++++-------------
- 2 files changed, 38 insertions(+), 74 deletions(-)

## feat(lane3): add bounded Gemini gate reporting
- tools/gemini/lane3-context/lane3_context_mcp.py  | 51 +++++++++++++++++++++---
- tools/lane/policies/gemini-lane3.toml            |  6 +++
- tools/lane/test_lane3_context_mcp.py             | 20 ++++++++--
- 4 files changed, 69 insertions(+), 10 deletions(-)

## fix(lane3): complete MCP policy catch-all
- tools/lane/policies/gemini-lane3.toml | 1 +
- 1 file changed, 1 insertion(+)

## fix(lane3): use scalar Gemini policy tool rules
- tools/lane/policies/gemini-lane3.toml | 58 ++++++++++++++++++++++++++++-------
- 1 file changed, 47 insertions(+), 11 deletions(-)

## fix(lane3): validate Gemini extension registration
- tools/lane/lane3                  | 15 ++++++++-------
- tools/lane/test_lane_launchers.py | 30 +++++++++++++-----------------
- 2 files changed, 21 insertions(+), 24 deletions(-)

## feat(lane3): add bounded Gemini context extension
- tools/lane/policies/gemini-lane3.toml              |  27 +++-
- tools/lane/test_lane3_context_mcp.py               | 114 ++++++++++++++++
- tools/lane/test_lane_launchers.py                  |  52 +++++++-
- 10 files changed, 428 insertions(+), 31 deletions(-)

## feat(lane): Gemini Lane 3 admin policy + pre-staged context (harmonic-forge#326)
- tools/lane/_agent_registry.sh         |  12 ++-
- tools/lane/lane3-stage-context        | 154 +++++++++++++++++++++++++++++++
- tools/lane/lane3_safety_additions.txt |  15 ++-
- tools/lane/policies/gemini-lane3.toml | 169 ++++++++++++++++++++++++++++++++++
- tools/lane/test_lane_launchers.py     |  85 +++++++++++++----
- 5 files changed, 410 insertions(+), 25 deletions(-)

## feat(canary): rebuild the deny-canary for Lane 3, fix #413's assertion defects (harmonic-forge#326)
- 3-lane-protocol.md                                 |  39 +-
- ...-agent-adapter-contract-and-capability-tiers.md |  20 +-
- tools/lane/policies/canary/run_canary.py           | 551 ++++++++++++++-------
- 3 files changed, 438 insertions(+), 172 deletions(-)

## fix(canary): kill the process group on timeout, per-check ceilings (harmonic-forge#326)
- tools/lane/policies/canary/run_canary.py | 277 +++++++++++++++++++++++--------
- 1 file changed, 207 insertions(+), 70 deletions(-)

## test(lane): capture pre-refactor launch tuples as the AC8 baseline (harmonic-forge#322)
- tools/lane/baseline_capture.py         | 314 ++++++++++++
- tools/lane/baseline_launch_tuples.json | 888 +++++++++++++++++++++++++++++++++
- tools/lane/lane3_safety_additions.txt  |  40 ++
- 3 files changed, 1242 insertions(+)

## feat(lane): closed agent registry, laneN --agent, Lane 3 check-only (harmonic-forge#322)
- tools/lane/_agent_registry.sh     | 259 +++++++++++++
- tools/lane/_cli_launch.sh         | 344 ++++++++++++-----
- tools/lane/_lane_args.sh          |  97 +++++
- tools/lane/baseline_capture.py    | 121 +++++-
- tools/lane/lane1                  |  24 +-
- tools/lane/lane2                  |  50 ++-
- tools/lane/lane3                  | 217 ++++++++---
- tools/lane/lane3-provision        | 101 +++++
- tools/lane/test_lane_launchers.py | 792 ++++++++++++++++++++++++++++++++++++++
- tools/run_tests.py                |   2 +-
- 10 files changed, 1812 insertions(+), 195 deletions(-)

## docs(lane): --agent is the documented interface; record #322's decisions (harmonic-forge#322)
- 3-lane-protocol.md                                 | 109 +++++++++++++++++----
- ...-agent-adapter-contract-and-capability-tiers.md |  90 ++++++++++++++---
- 2 files changed, 166 insertions(+), 33 deletions(-)

## fix(tooling): resolve model tier gate issue targets (harmonic-forge#367)
- ...-agent-adapter-contract-and-capability-tiers.md |  2 +-
- tools/hooks/model_tier_gate.py                     | 89 +++++++++++-----------
- tools/hooks/test_model_tier_gate.py                | 85 +++++++--------------
- 6 files changed, 90 insertions(+), 108 deletions(-)
## fix(tooling): harden permission hook command classification (harmonic-forge#369)
- tools/hooks/batch_auth.py             | 20 +++++++++++++++++++-
- tools/hooks/block_irreversible_ops.py | 16 ++++++++++++----
- tools/hooks/shell_parse.py            | 21 +++++++++++++++++++++
- 4 files changed, 60 insertions(+), 5 deletions(-)

## fix(tooling): transaction-log.md merge=union to end structural branch conflicts (harmonic-forge#376)
- .gitattributes | 15 +++++++++++++++
- 1 file changed, 15 insertions(+)

## feat(tooling): Lane 2 status-post integrity -- receipts, wrapper-only posting (harmonic-forge#371)

Reimplements harmonic-forge#371 after a prior Lane 2 (Codex) attempt got
to 'L2 blocked' with only receipt_runner.py/l2_post.py/the deny hook as
dense, likely-incomplete stubs, and that branch also carried the same
unrelated sprint-plan config_loader deletion seen on #366's Codex
attempt -- discarded, not merged from, same reasoning as that issue.

Adds tools/gh/receipt_runner.py (issue-scoped, content-digest command
receipts under .git/lane2-receipts/<issue>/, lock-on-nonzero-exit),
tools/gh/l2_post.py (post/snapshot/resolve-lock subcommands; posting
requires a mandatory post/fetch/diff self-check before reporting
success, and refuses ordinary status composition while an issue is
locked, except a legitimate --kind blocked), and
tools/hooks/block_lane2_status_claims.py (repo-agnostic raw-post deny
for LANE=2, reusing block_lane1_status_claims.py's is_direct_transport
rather than reimplementing it). Wired for Claude Code
(.claude/settings.json) and Codex (new .codex/hooks.json, pointed
directly at the canonical module -- harmonic-forge has no per-repo
gate_codex_tool.py the way HRSE2 does). Four new mise tasks
(l2-run/l2-post/l2-snapshot/l2-resolve-lock). ADR-007 Sec7 gains the new
guard-equivalence row.

27 new unit tests (490 total, up from 463), all passing. Live-verified
end to end against a disposable throwaway issue (harmonic-forge#373,
closed): real receipt-backed command -> real GitHub post with a
passing self-check -> a genuine failing command creating a real lock
-> a normal completion post correctly refused while locked -> a
legitimate blocked status correctly bypassing the lock -> resolve-lock
correctly refused against a fake comment id and correctly clearing
against the real one. The deny hook's decision() is exhaustively unit
tested (13 cases: LANE 2/1/3/unset, two different target repos, cd
prefixing, malformed payload) and its CLI entrypoint verified against
hand-built Claude- and Codex-shaped stdin payloads.
- tools/gh/test_receipt_runner.py                    |  69 +++++++++
- tools/hooks/block_lane2_status_claims.py           | 101 +++++++++++++
- tools/hooks/test_block_lane2_status_claims.py      | 110 +++++++++++++++
- 10 files changed, 752 insertions(+)

## fix(tooling): bake the {summary,findings} reply contract into cross_family_call.sh itself (harmonic-forge#366)

Fixes the defect Lane 1 found live while building #366's Lane 3 gate
sweep evidence: a probe brief with no explicit reply-shape instruction
produced a fully correct plain-prose answer from Codex that
emit_envelope's codex branch then classified invalid-report, because
nothing in invoke_codex() or the caller's brief told Codex to answer in
the shared JSON contract. Every correct Codex answer was failing,
silently, for every caller except Codex itself -- TC1's 2-of-3 vote,
and this issue's whole premise, depended on this working.

Fix applies to all three families uniformly, not just Codex: a fixed
REPORT_CONTRACT suffix is now appended to every brief inside
prompt_text(), so a caller gets a working contract even if it never
thinks to ask for one, rather than depending on brief-authoring
discipline the helper cannot enforce.

Reproduced live before and after: same bare brief (no JSON-shape
instruction), same seeded-defect scratch repo. Before: Codex target ->
status invalid-report despite a fully correct diagnosis in its native
output. After: Codex and Gemini targets both -> status ok with the
seeded defect correctly quoted as evidence. Re-verified TC2 (Gemini
read-only write denial) unaffected by the change, isolated from the
same-cwd cross-contamination a naive re-test would have introduced.
mise run check -- 463 tests pass.
- tools/lane/cross_family_call.sh | 26 +++++++++++++++++++++++---
- 1 file changed, 23 insertions(+), 3 deletions(-)

## feat(tooling): cross-family headless call helper -- ADR-007 amendment, admin-policy Gemini boundary (harmonic-forge#366)

Reimplements harmonic-forge#366 after Lane 1 sent the prior Lane 2 (Codex)
attempt back: the read-only posture's Gemini boundary used
--approval-mode plan, which live reproduction showed does not reliably
block a write (the model can still call write_file and narrate a false
success). Replaced with an admin-tier deny policy
(tools/lane/gemini-read-only-deny.toml, --admin-policy), the mechanism
harmonic-forge#326 already proved survives --yolo and removes denied
tools from the model's tool list entirely.

Adds tools/lane/cross_family_call.sh (closed caller/families/posture/
brief/cwd surface, locked caller-keyed family order, normalized
JSON-lines envelope per family), the pager/editor env fix to
_cli_launch.sh's gemini branch, and a bounded ADR-007 section
transcribing the family-order table, posture mapping, and consumer
table.

Also carries a new accepted-residual-gap note: live testing found
'codex exec --sandbox read-only' does not reliably block a file write
either (the file-edit tool completes even when shell redirection is
denied at the OS level) -- out of this issue's Gemini-scoped acceptance
criteria, flagged rather than silently fixed or ignored.
- tools/lane/_cli_launch.sh                          |   4 +
- tools/lane/cross_family_call.sh                    | 191 +++++++++++++++++++++
- tools/lane/gemini-read-only-deny.toml              |  36 ++++
- 4 files changed, 334 insertions(+)

## fix: validate sprint-plan schema contract (harmonic-forge#104)
- skills/sprint-plan/config_loader.py                |  89 ++++++-----
- .../schema/sprint-plan.config.schema.json          |  55 ++++++-
- skills/sprint-plan/test_config_loader.py           | 163 ++++++++++++++++-----
- 3 files changed, 234 insertions(+), 73 deletions(-)

## feat: add sprint-plan config loader (harmonic-forge#104)
- skills/sprint-plan/config_loader.py                | 107 +++++++++++++++++++++
- .../schema/sprint-plan.config.schema.json          |   1 +
- skills/sprint-plan/test_config_loader.py           |  48 +++++++++
- 3 files changed, 156 insertions(+)

## feat: add --theme and --venture flags to gh_issue.py (F308)

Generalizes _set_tier() into a shared _set_single_select() (Tier/Theme/Venture no longer three copies of the same match/edit/error shape), threads --theme/--venture through add_to_board() with the same hard-fail-if-requested-but-unwritten semantics Tier already has, reads option lists live from the board rather than hardcoding them (harmonic-forge#300), and prints the real available options on an unmatched value. HRSE2's gh-new-issue wrapper updated in the same change; mise run wrapper-parity passes. 13 new tests (48 total, up from 35), all passing. Live-verified against both boards (hrse #1, forge #3) including the invalid-value error path; three throwaway issues created and closed.
- tools/gh/README.md        |  12 +++-
- tools/gh/gh_issue.py      | 119 +++++++++++++++++++++++++++---------
- tools/gh/test_gh_issue.py | 150 ++++++++++++++++++++++++++++++++++++++++++++++
- 3 files changed, 250 insertions(+), 31 deletions(-)

## fix: resolve BASH_SOURCE through its symlink in lane1/2/3 (F305 regression)

~/.local/bin/lane{1,2,3} are symlinks into this directory; BASH_SOURCE[0] reflects the invoked path, not the symlink target, so dirname resolved to ~/.local/bin instead of here and the new source line 404'd -- broke all three launchers live. readlink -f before dirname fixes it; verified through the actual ~/.local/bin symlinks, not just the real files.
- tools/lane/lane1 | 2 +-
- tools/lane/lane2 | 2 +-
- tools/lane/lane3 | 2 +-
- 3 files changed, 3 insertions(+), 3 deletions(-)

## feat(gh): repo hygiene backstop — four remaining gaps (hrse#808)

Adds three local-checkout audits (checkout not on main, stale stashes,
merged commits missing a transaction-log.md entry) and fixes
_BOARD_ITEMS_QUERY's hardcoded `user(login:)` GraphQL owner type — it now
tries organization first, falls back to user, so a future org-owned
client-repo instance resolves correctly instead of silently 404ing.

The transaction-log check went through a live-verified redesign mid-
implementation: an initial pure headline-match version produced 524/588
false positives on hrse, because a squash-merged PR's own final subject
is the PR title, never any local commit's message, so it rarely matches
a documented headline even when `mise run commit` genuinely ran somewhere
in the PR. Fixed to also accept "this commit's own diff touched
transaction-log.md at all" as an independent, sufficient signal.

The owner-type fallback also needed a second live fix: `gh api graphql`
exits nonzero for `organization(login:)` against a real user-owned login
rather than returning a graceful null, so the fallback's first version
never actually reached the user() attempt — silently skipping the
migration/unboarded sweep on every repo, every run, until caught live.
- tools/gh/repo_hygiene.py      | 275 +++++++++++++++++++++++++++++++--
- tools/gh/test_repo_hygiene.py | 344 ++++++++++++++++++++++++++++++++++++++++++
- 2 files changed, 607 insertions(+), 12 deletions(-)

## tooling: scope gh CLI active-account state per-project via GH_CONFIG_DIR (F305)

lane1/2/3 now export GH_CONFIG_DIR based on the target project's git remote, so a global gh auth switch in one project can no longer lock out a concurrent lane session in another. Verified live against both existing account dirs (gh-vitalharmony, gh-harmonicarchitect) and the no-match fallthrough case.
- tools/lane/lane1             |  2 ++
- tools/lane/lane2             |  2 ++
- tools/lane/lane3             |  2 ++
- 4 files changed, 32 insertions(+)

## docs: document gh-as in foundation documents and directives (harmonic-forge#240)

gh-as was documented in tools/gh/README.md and the root README.md — enough
for someone who goes looking, not enough for a tool that governs credential
safety in multi-person and multi-agent work. The actors most likely to reach
for gh auth switch are the ones who never read tools/gh/README.md: agents
that load only universal-agent.md and 3-lane-protocol.md, and developers
following §6 onboarding.

- rules/universal-agent.md: the directive itself, in SECURITY, since this is
  the file agents actually load
- 3-lane-protocol.md: agent-readable standing rule for every lane, in the
  same register as GitHub Comment Formatting
- harmonic-forge.md §3: gh-as in the platform repo structure
- harmonic-forge.md §6: names gh-as as the mechanism enforcing the
  credential-isolation principle already stated there — gh auth switch
  cannot be part of that answer, since global identity mutation is exactly
  the cross-project bleed the principle exists to prevent
- harmonic-forge.md §6 onboarding: one-time gh-as --init setup step
- docs/onboarding-greg.md: the multi-account case, in the Security Model
  section he is told to read first — highest-risk case on the platform
- docs/onboarding-kyle.md: brief pointer; single-account today

Corpus swept: every remaining mention of gh auth switch is a warning against
it, none recommend it.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
- [docs] Markdown-only commit — no code changes. Files: 3-lane-protocol.md, docs/onboarding-greg.md, docs/onboarding-kyle.md, harmonic-forge.md, rules/universal-agent.md

## tools/gh: add gh-as — per-process GitHub account scoping (harmonic-forge#235)

gh auth switch is global mutable state: switching accounts for one project
silently changes the active identity for every other concurrent session on
the machine, and there is no reliable way to restore it when two sessions
are running. Observed live 2026-08-11 — an account switch was reverted by a
different session mid-task, surfacing as a confusing error.

gh-as scopes gh to a named account per process via a per-account
GH_CONFIG_DIR under ~/.config/gh-accounts/<account>. Nothing to undo: the
scoping lives and dies with the process, so concurrent sessions cannot
collide. This is what harmonic-forge.md §6's credential-isolation principle
already required; there was no sanctioned mechanism for it until now.

Guards on every invocation: refuses an unconfigured slot, refuses an expired
or revoked token, and refuses when a slot's authenticated identity does not
match its name — so a command can never run against the wrong account
because a token was replaced out of band.

Documented in tools/gh/README.md and in README.md's credential-isolation
section as the sanctioned alternative to gh auth switch.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
- README.md          | 13 ++++++++
- tools/gh/README.md | 29 ++++++++++++++++--
- tools/gh/gh-as     | 89 ++++++++++++++++++++++++++++++++++++++++++++++++++++++
- 3 files changed, 128 insertions(+), 3 deletions(-)

## feat: deny Lane 3 from posting its own AE authorization comment (harmonic-forge#216)

New PreToolUse hook, sibling to remind_gate_readiness_sweep.py: denies a
LANE=3 session from posting a comment body matching the AE-authorization
heading shape, across both the wrapper posting tasks (lane-comment,
post-comment, post_lane_discussion.py) and raw gh issue comment --body/
--body-file -- the latter matters because raw gh is explicitly permitted
for Lane 2/3 and would otherwise be a silent, unmarked bypass.

Fenced-code-block stripping added after the test suite caught a real gap:
the heading-anchor regex alone denied a comment that only *quoted* the AE
format inside a markdown fence (this very issue's own body does exactly
that) -- fixed and re-verified before committing.
- tools/hooks/deny_lane3_ae_self_post.py      | 199 ++++++++++++++++++++++++++++
- tools/hooks/test_deny_lane3_ae_self_post.py | 192 +++++++++++++++++++++++++++
- 2 files changed, 391 insertions(+)

## docs: bound live verification of pre-existing services (forge#132)
- [docs] Markdown-only commit — no code changes. Files: 3-lane-protocol.md

## feat(agents): add read-only advisory profiles and sync them to .devin/agents
- agents/sticky-wicket/AGENT.md    |  69 ++++++++++++++++
- rules/universal-claude.md        | 165 ++++++++++++++++++++-------------------
- sync_rules.py                    |  73 ++++++++++++++++-
- 5 files changed, 362 insertions(+), 84 deletions(-)

## docs: codify gitignored-local-file pattern for passing sensitive real-world data between lanes
- [docs] Markdown-only commit — no code changes. Files: rules/universal-agent.md, templates/lane1-handoff.md

## docs: harmonic-forge.md's repo-layout tree was missing tools/gh/, .githooks/, mise.toml, transaction-log.md
- [docs] Markdown-only commit — no code changes. Files: harmonic-forge.md

<!-- TRANSACTION_LOG_END -->
