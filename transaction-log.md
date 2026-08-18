# harmonic-forge Transaction Log

Auto-maintained by `mise run commit` (`scripts/git_commit.py` + `tools/transaction-log/`) — appends a delta summary in the same commit as the code change it describes (headline = verbatim commit message). Cleared on **push to main**, not a version bump — this repo has no running artifact to stamp, so push is its genuine "publish" event (see `mise.toml`'s header comment). Full history: `git log -p transaction-log.md`. Read this file at session start for recent context. Do not edit by hand.

<!-- TRANSACTION_LOG_START -->
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
