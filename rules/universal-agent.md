# Universal Agent Directives (ALL PROJECTS)

Canonical process rules for every AI agent (Claude Code, Codex, and any
operator-assigned runtime) on every Vital Harmony project. Project-level
`.windsurfrules` may add constraints but may not weaken these. Precedence:

```
harmonic-forge/rules/universal-*.md
  → harmonic-forge/rules/{language}.md
    → {project}/.windsurfrules  (project overrides)
      → {project}/CLAUDE.md     (session-specific context)
```

## SERVICE LIFECYCLE & GIT (NON-NEGOTIABLE)

- Every project designates exactly one service-lifecycle path (its project
  `.windsurfrules` names it — e.g. HRSE2's `mise run restart`/`check`/`bump`/
  `commit` tasks, adopted 2026-07-13 per ADR-001, replacing a prior custom
  script). All restarts, commits, version bumps, and pushes route through it.
  No agent manually runs `git add`/`commit`/`push`, or manually starts/stops
  dev servers, as a substitute.
- If the lifecycle tool fails, diagnose and fix the tool — do not bypass
  it with manual commands.
- Push to the remote only when the human operator explicitly asks.

## VERIFICATION GATE (HARD REQUIREMENT)

Before any commit-producing restart, the project's full verification gate
(lint + typecheck/build + backend type-check, at minimum) must pass. Fix
failures first — never commit broken code to satisfy a deadline.

Run gate commands one at a time, as separate tool calls, with absolute paths.
Never chain with `&&`/`||`/`;`/pipes. Wait for each result before proceeding.

## MODULARITY (SUB-300 LINES)

If an edit would push any source file over 300 lines, halt and decompose into
smaller modules before proceeding. Composition over monoliths. One-off/
migration scripts in a project's designated scripts directory are exempt.

## SECURITY

- Secrets never appear in code, logs, prompts, or commit messages.
- Any query language with injection risk (SQL, Cypher, etc.) must be
  parameterized — never build queries via string interpolation/concatenation.
- Any single-instance resource (DB driver, connection pool) is a module-level
  singleton owned by one file — no agent instantiates a second one elsewhere.

### GitHub account scoping — never `gh auth switch`

`gh auth switch` mutates **global** state. It changes the active GitHub
identity for every other session and agent on the machine, not just yours.
On a multi-lane machine that is a cross-session failure: another actor's
command silently runs, or fails, under an identity it never chose. Real
incident, 2026-08-11 — an account switch was reverted by a concurrent
session mid-task and surfaced as a confusing error rather than an obvious
one.

**Use `tools/gh/gh-as` instead.** It scopes `gh` to a named account for one
process via a per-account `GH_CONFIG_DIR`, so there is nothing to undo and
nothing to collide with:

```bash
gh-as <account> gh issue list -R owner/repo
gh-as <account> python3 some_script.py     # child processes inherit the scoping
```

If a script needs a specific account, do not switch inside it — have the
caller invoke it under `gh-as`, and have the script **verify** its identity
(`gh api user --jq .login`) and refuse if it is wrong. This is the
credential-isolation principle in `harmonic-forge.md` §6 made mechanical:
one project's identity must never be able to act on another's.

### Passing sensitive real-world data between lanes

Real business/personal data (negotiation figures, contact details, PII,
anything that isn't a credential but still shouldn't become a permanent
public-or-semi-public record) must never be pasted into a GitHub issue
comment, PR description, or any other artifact that persists and is
readable by every repo collaborator — even on a private repo, since
collaborator access doesn't imply the data's own sensitivity clearance
(a project's `hrse` repo, say, may have a collaborator who has no reason
to see the operator's live salary-negotiation figures).

Pattern, live-verified on HRSE2 (hrse#226's seed-import data, 2026-07-14):
1. Write the sensitive data to a file under a project-local, gitignored
   directory (e.g. `scripts/seed_data/`) — add the directory to
   `.gitignore` explicitly if it isn't already covered, and verify with
   `git check-ignore -v <path>` before trusting it.
2. Post a comment on the relevant issue that names the **file path only**
   — never the content — plus enough structural metadata (row count,
   format, known open questions) for the next lane to use it without
   re-deriving context. The comment is safe to keep permanently; the data
   file is not committed, ever.
3. The next lane (human or agent) reads the file directly from local disk.
   This only works when both lanes share a filesystem (true for Lane
   1/2/3 on a single operator's machine) — if lanes run on genuinely
   separate machines, this pattern doesn't apply and a different
   transfer mechanism is needed.

This is a sibling rule to "no agent writes to a `.env`/secrets file"
below, not the same rule: `.env` never gets agent-written because secrets
management is deliberately human-only; sensitive *data* files, by
contrast, are fine for an agent to write (it's not a credential), the
constraint is purely about where the content is allowed to become durably
visible.

## GITHUB API — PREFER REST OVER GRAPHQL (harmonic-forge#242)

Locked in as standing policy 2026-08-11, after the shared account's
GraphQL quota (5000/hr) was fully exhausted three times in one day.
GraphQL cost is complexity-based, not call-count-based — a single `gh
project item-list --limit 1000` can burn a large fraction of the hourly
budget on its own (confirmed live, harmonic-forge#203) — and that quota
is shared across every concurrent lane/session on the account, so one
session's board-heavy work can silently exhaust the bucket another
session needs for a routine issue lookup. REST draws from a separate,
much larger quota and does not contend with GraphQL-heavy work happening
elsewhere.

**Use the REST form whenever one exists**, for ad-hoc lookups in any
lane's own session, not just inside shared tooling scripts:

| Action | REST (use this) | GraphQL-backed (avoid) |
|---|---|---|
| View an issue / list comments | `gh api repos/OWNER/REPO/issues/N`, `.../issues/N/comments` | `gh issue view` |
| Post a comment | `gh api repos/OWNER/REPO/issues/N/comments -f body="$(cat file.md)"` | `gh issue comment` |
| Close / reopen an issue | `gh api repos/OWNER/REPO/issues/N -X PATCH -f state=closed` | `gh issue close` |
| Create an issue | `gh api repos/OWNER/REPO/issues -f title=... -F body=@file -f "labels[]=X"` | `gh issue create` |
| Create a PR | `gh api repos/OWNER/REPO/pulls -f title=... -f head=... -f base=... -F body=@file` | `gh pr create` |
| Merge a PR | `gh api -X PUT repos/OWNER/REPO/pulls/N/merge -f merge_method=squash` | `gh pr merge` |
| Check CI status | `gh api repos/OWNER/REPO/commits/SHA/check-runs` | `gh pr checks` |
| Delete a branch | `gh api -X DELETE repos/OWNER/REPO/git/refs/heads/BRANCH` (URL-encode slashes) | GraphQL-based deletion |

Note on issue creation: `gh_issue.py`'s board-add step still uses GraphQL
(`gh project item-add`) even after the issue-creation half moves to REST
— the substitution above only covers the create call itself.

**The one confirmed exception: Projects v2 board operations
(`item-list`/`item-add`/`item-edit`/`field-list`) have no REST
equivalent** — these remain GraphQL by necessity, not preference. Keep
board reconciliation batched and cached (`board_sync.py`'s delta-sync and
field-ID caching, hrse#386/harmonic-forge#203) rather than trying to
avoid GraphQL for these calls.

Rate-limit self-check: `gh api rate_limit --jq '.resources.graphql'`
before a batch of board-heavy operations. Prefer reading the
`X-Ratelimit-*` response headers on a real request over trusting a bare
`gh api /rate_limit` call in isolation — two live reads have been observed
to disagree in this environment; the headers on an actual request are the
more trustworthy source.

Full investigation, live measurements, and the decomposed follow-up
issues (harmonic-forge#218 epic, #219–223): the research briefing at
`~/Harmonic_Projects/research/2026-08-10-gh-graphql-rate-limit-briefing.md`.

## CLOUD-NATIVE & 12-FACTOR READINESS

Target: every project survives a clean forklift to its cloud target.

- **Config:** zero hardcoded IPs, ports, URIs, keys, or model IDs — read from
  environment variables only. No `localhost` defaults baked into production
  paths.
- **Statelessness:** don't write user uploads to local disk if the project is
  meant to run in a stateless/containerized environment — design for
  object storage instead.
- **Observability:** no raw `print()`/`console.log()` debugging left in
  production paths — use the project's structured logger.
- **Multi-tenancy:** never hardcode a user/tenant identifier; context flows
  from the authenticated session.

## AI ABSTRACTION (LLM GATEWAY)

If a project makes LLM calls, all of them route through one designated
gateway module. No other module constructs a provider client directly or
hardcodes a model ID. Model/tier selection is env-first. LLM output is
untrusted input — parse and validate it, never `eval` it or raw-string-match
it for control flow.

## LIVE VERSIONED VALUES (MODEL IDS, API VERSIONS, SDK SIGNATURES)

Any value that names a specific version of an external, actively-evolving
system — a model ID string, an API version, an SDK method signature/import
path — is not something training data can be trusted to produce correctly.
These strings look exactly as plausible when fabricated as when real, and
training data goes stale the moment a vendor ships a new version.

- If a handoff or fix requires such a value and it is not already present
  elsewhere in the codebase as a working reference, verify it live (vendor
  docs, WebFetch/WebSearch, or an existing skill's authoritative reference
  file) before writing it — never recall it from training.
- Default to preserving the existing working value unless the task
  explicitly calls for a version change. A handoff that doesn't mention
  changing a model/version string is not license to "correct" it — that is
  scope creep on top of an unverified guess, the worst combination.
- If verification isn't possible in the moment (no web access, no
  authoritative reference), say so explicitly and flag the value as
  unverified rather than silently substituting a plausible-looking guess.

## API ABSTRACTION (FRONTEND)

If a project has a frontend that calls its own backend, all such calls route
through one designated HTTP client module. No raw `fetch()`/axios calls
scattered elsewhere. Components degrade gracefully (mock data / empty states)
when the backend is offline rather than crashing.

## UTILITY / MIGRATION SCRIPTS

Check the project's designated scripts directory before writing a new
one-off. Never place single-use scripts inside the application source tree
(backend or frontend). Prefix single-use scripts so they sort together and
read as disposable (e.g. `1-fix_nodes.py`).

## BUG-FIX PROTOCOL (ALL AGENTS)

Every bug fix — implementing or reviewing — follows read-propose-execute.
Skipping a step is a protocol violation.

1. **Read before touching.** Locate and read the exact file sections involved
   before opening an editor.
2. **Propose before executing.** State in plain text: the root cause (not
   just the symptom), the exact change (which lines, what replaces what), and
   the expected outcome (what log line / API response / state confirms it
   worked).
3. **Execute, then verify against a stated test case.** Every bug-fix must
   include: "After fixing, [action] should produce [Y] and should NOT produce
   [Z]." Run that exact test against the live service — not just a re-read of
   the diff. See `rules/testing-gate.md` for what counts as live verification.

If fixing multiple bugs, address each as a numbered item. Do not infer or
invent a bug that was not explicitly described.

**No ad-hoc fixes, ever — this includes data, not just code.** A bug always
gets a tracked issue and goes through this protocol and the 3-lane loop,
full stop — never a quick fix applied in the moment because it's small,
convenient, or already in front of you. This applies even when the "fix" is
just deleting or editing bad data through an existing UI affordance (e.g. an
already-provided delete button) rather than touching code: correcting the
symptom outside the tracked, verified pipeline is exactly as much a
violation as patching source directly. If you notice bad data while
diagnosing a bug, describe it in the issue as evidence, then leave it alone
— the fix (including any data correction) ships through the same
implementation + Lane 3 verification as the code change, not as a side
action while you happen to be looking at it.

**No agent ever writes to a `.env`/secrets file, under any circumstance.**
This is a third category alongside application code and data — not covered
by treating it as either. A handoff that requires a new required env value
(e.g. a secret key with no safe default) states that as a **blocking
prerequisite for the human operator to complete**, never as something the
implementing agent generates or appends itself, even with a
plausible-looking value (e.g. `openssl rand -hex 32`) and even when the
handoff already names the exact command for the human to run. If the value
isn't present yet, report that and stop — do not add it "to keep going."
Real incident: Lane 2 wrote its own `SESSION_SECRET_KEY` into
`backend/.env` on HRSE2 issue #175 despite the handoff explicitly stating
the human operator was adding it, producing two conflicting values for the
same key in a secrets file.

**This extends to hardcoding a real secret value into any non-`.env` file**
— compose files, config YAML, scripts, anything that will be committed.
Real incident: on HRSE2 #176, Lane 3 hit a compose variable-interpolation
problem (`${NEO4J_PASSWORD}` not expanding) and, instead of fixing the
invocation or stopping to report it, wrote the actual live password as a
plaintext literal into `podman-compose.yml` — a file about to be committed
to git. Caught by Lane 1 before any commit landed, but this is the same
violation as writing to `.env` directly, just in a different file. If a
secret needs to flow through a config file, it must arrive via variable
interpolation/env injection at runtime, never a literal value written by an
agent — full stop, regardless of which file.

## STANDING-RULE VIOLATIONS GET FILED, NOT FIXED, NOT JUST MENTIONED

If Lane 2 (implementing) or Lane 3 (testing/style pass) encounters a
violation of any standing rule — modularity, type safety, security/DB
patterns, cloud-native/12-factor, observability, multi-tenancy, any rule in
this file or the project's own rule files — in a file already being touched
for the current issue, it must file a new, separate tracked issue describing
the violation (file, line, rule violated) rather than fixing it inline or
only mentioning it in a gate comment. A gate comment closes with its parent
issue and the finding is lost; a filed issue persists and can be prioritized
independently. This extends the existing report-only/no-self-fix discipline
(the style-pass rule above, and the incidents that produced it) to *all*
standing-rule categories, not just the 300-line cap.

This is also the replacement for any project's periodic manual audit gate
(e.g. HRSE2's retired `mini-audit.md`, a pre-3-lane-protocol artifact from
when monolithic files were a real, recurring problem): continuous per-issue
enforcement plus durable tracking of what's missed is a better fit than a
separate manual gate requiring a different tool to run. Marc: "That was an
artifact of the legacy of when I started before the 3-lane protocol, what
Cascade was building monoliths. I'm ok with dumping it completely." Known,
accepted gap: this doesn't catch slow drift in a file nobody happens to
touch again after crossing a threshold — a lower-value case better solved
by future codebase-wide pattern detection (see the `harmonic-forge` Epic #11
knowledge-graph work) than a manual periodic sweep.

## MEMORY-ENTRY STANDARD (ALL AGENTS, ALL MEMORY SURFACES)

A durable memory entry — a rule addition to any `harmonic-forge` doc, an agent
memory file, or a Lane 3 `AGENTS.md` self-correction — earns its place by
completing the five-rung ladder, not stopping partway:

1. **Fail** — something went wrong; write down what happened.
2. **Investigate** — figure out why, don't assume.
3. **Verify** — turn the diagnosis into a checked fact (re-tested, re-read,
   independently confirmed — not just "seems right").
4. **Distill** — turn the verified fact into a general rule, not just a
   record of the one-off incident.
5. **Consult** — write it so a future session reads the rule instead of
   re-deriving it from scratch.

Practical entry discipline: a memory entry states **FAILED / WHY / VERIFIED
/ RULE**. If a step wasn't done — the cause wasn't actually investigated, or
the fix wasn't verified live — say so, or don't store the entry as settled
fact. A guess marked as a guess is fine; a guess stored as a verified rule is
the failure mode this standard exists to prevent.

This applies explicitly to every Lane 3 runtime's agent-maintained standing
instructions and memory surfaces — they need the same entry-quality bar to
avoid becoming a pile of unverified failure notes.

Does **not** apply to a project's `transaction-log.md` (or equivalent delta
log) — that is rung-1-by-design, a record of what changed, not a memory. The
ladder applies to whatever gets promoted *out of* a delta log into a real
rule, not to the log itself.

## SURGICAL CHANGES (KARPATHY PRINCIPLE)

Make the minimum change that accomplishes the stated task. Do not refactor,
rename, reorganize, or clean up adjacent code as part of a bug fix or small
feature — that is a separate task and a separate commit.

- A bug fix touches only the lines needed to fix the root cause.
- A small feature adds only what was asked for — no "while I'm here" cleanup.
- Pre-existing style issues, unused imports, or long lines you didn't
  introduce: leave them, unless the task is explicitly "clean up X."
- If decomposition is genuinely required to touch a file safely (it already
  exceeds the 300-line cap), say so explicitly before doing it, then do only
  that, then make the targeted change.

## ISSUE TRACKING

New bugs and features are tracked as GitHub Issues on the project's repo, not
appended to a local backlog file. Close issues directly on GitHub when work
completes and is verified.
