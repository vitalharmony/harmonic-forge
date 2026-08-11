# harmonic-forge Transaction Log

Auto-maintained by `mise run commit` (`scripts/git_commit.py` + `tools/transaction-log/`) — appends a delta summary in the same commit as the code change it describes (headline = verbatim commit message). Cleared on **push to main**, not a version bump — this repo has no running artifact to stamp, so push is its genuine "publish" event (see `mise.toml`'s header comment). Full history: `git log -p transaction-log.md`. Read this file at session start for recent context. Do not edit by hand.

<!-- TRANSACTION_LOG_START -->
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
