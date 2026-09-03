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

<!-- R-0001 -->
- Every project designates exactly one service-lifecycle path (its project
  `.windsurfrules` names it — e.g. HRSE2's `mise run restart`/`check`/`bump`/
  `commit` tasks, adopted 2026-07-13 per ADR-001, replacing a prior custom
  script). All restarts, commits, version bumps, and pushes route through it.
  No agent manually runs `git add`/`commit`/`push`, or manually starts/stops
  dev servers, as a substitute.
<!-- /R-0001 -->
<!-- R-0002 -->
- If the lifecycle tool fails, diagnose and fix the tool — do not bypass
  it with manual commands.
<!-- /R-0002 -->
<!-- R-0003 -->
- Push to the remote only when the human operator explicitly asks.
<!-- /R-0003 -->

## VERIFICATION GATE (HARD REQUIREMENT)

<!-- R-0004 -->
Before any commit-producing restart, the project's full verification gate
(lint + typecheck/build + backend type-check, at minimum) must pass. Fix
failures first — never commit broken code to satisfy a deadline.
<!-- /R-0004 -->

<!-- R-0005 -->
Run gate commands one at a time, as separate tool calls, with absolute paths.
Never chain with `&&`/`||`/`;`/pipes. Wait for each result before proceeding.
<!-- /R-0005 -->

## MODULARITY (SUB-300 LINES)

<!-- R-0006 -->
If an edit would push any source file over 300 lines, halt and decompose into
smaller modules before proceeding. Composition over monoliths. One-off/
migration scripts in a project's designated scripts directory are exempt.
<!-- /R-0006 -->

## SECURITY

<!-- R-0007 -->
- Secrets never appear in code, logs, prompts, or commit messages.
<!-- /R-0007 -->
<!-- R-0008 -->
- Any query language with injection risk (SQL, Cypher, etc.) must be
  parameterized — never build queries via string interpolation/concatenation.
<!-- /R-0008 -->
<!-- R-0009 -->
- Any single-instance resource (DB driver, connection pool) is a module-level
  singleton owned by one file — no agent instantiates a second one elsewhere.
<!-- /R-0009 -->
<!-- R-0010 -->
- **Never decrypt a secrets file and view its content wholesale** — not with
  `cat`/`head`, not briefly, not "just to check the structure." Pipe the
  decrypted stream directly into an extractor that emits only non-sensitive
  fields (key/field names, never values) in the same command, so plaintext
  never reaches a shell buffer or transcript.
<!-- /R-0010 -->
<!-- R-0011 -->
- **"Give me the command to do X" is a request for the command, not
  authorization to run X** — holds with extra force when X touches a
  credential, and for the read half too (fetching/printing a value "to be
  helpful" is the same overstep at smaller scale). Hand over the command
  text and stop.
<!-- /R-0011 -->
<!-- R-0012 -->
- **Never set a global git credential helper.** `credential.helper=store`
  silently caches *any* successful HTTPS auth across every repo on the
  machine — a client-scoped PAT leaked into an unrelated account's push
  this way. Use `gh auth setup-git`, which scopes credentials per host
  through `gh`'s own helper — this governs git's own credential cache,
  distinct from the `gh` active-identity scoping below.
<!-- /R-0012 -->
<!-- R-0013 -->
- **Never state a process rule as fact without checking the doc it comes
  from.** A confidently-recalled rule that is subtly wrong (a lane count,
  an ordering, a flag name) is worse than admitted uncertainty — verify
  against the actual protocol text before asserting it, the same standard
  `rules/testing-gate.md` already sets for live-system verification.
<!-- /R-0013 -->

### GitHub account scoping — never `gh auth switch`

`gh auth switch` mutates **global** state. It changes the active GitHub
identity for every other session and agent on the machine, not just yours.
On a multi-lane machine that is a cross-session failure: another actor's
command silently runs, or fails, under an identity it never chose. Real
incident, 2026-08-11 — an account switch was reverted by a concurrent
session mid-task and surfaced as a confusing error rather than an obvious
one.

<!-- R-0014 -->
**Use `tools/gh/gh-as` instead.** It scopes `gh` to a named account for one
process via a per-account `GH_CONFIG_DIR`, so there is nothing to undo and
nothing to collide with:
<!-- /R-0014 -->

```bash
gh-as <account> gh issue list -R owner/repo
gh-as <account> python3 some_script.py     # child processes inherit the scoping
```

<!-- R-0015 -->
If a script needs a specific account, do not switch inside it — have the
caller invoke it under `gh-as`, and have the script **verify** its identity
(`gh api user --jq .login`) and refuse if it is wrong. This is the
credential-isolation principle in `harmonic-forge.md` §6 made mechanical:
one project's identity must never be able to act on another's.
<!-- /R-0015 -->

### Passing sensitive real-world data between lanes

<!-- R-0016 -->
Real business/personal data (negotiation figures, contact details, PII,
anything that isn't a credential but still shouldn't become a permanent
public-or-semi-public record) must never be pasted into a GitHub issue
comment, PR description, or any other artifact that persists and is
readable by every repo collaborator — even on a private repo, since
collaborator access doesn't imply the data's own sensitivity clearance
(a project's `hrse` repo, say, may have a collaborator who has no reason
to see the operator's live salary-negotiation figures).
<!-- /R-0016 -->

Pattern, live-verified on HRSE2 (hrse#226's seed-import data, 2026-07-14):
<!-- R-0017 -->
1. Write the sensitive data to a file under a project-local, gitignored
   directory (e.g. `scripts/seed_data/`) — add the directory to
   `.gitignore` explicitly if it isn't already covered, and verify with
   `git check-ignore -v <path>` before trusting it.
<!-- /R-0017 -->
<!-- R-0018 -->
2. Post a comment on the relevant issue that names the **file path only**
   — never the content — plus enough structural metadata (row count,
   format, known open questions) for the next lane to use it without
   re-deriving context. The comment is safe to keep permanently; the data
   file is not committed, ever.
<!-- /R-0018 -->
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

<!-- R-0019 -->
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
<!-- /R-0019 -->

Note on issue creation: `gh_issue.py`'s board-add step still uses GraphQL
(`gh project item-add`) even after the issue-creation half moves to REST
— the substitution above only covers the create call itself.

**The one confirmed exception: Projects v2 board operations
(`item-list`/`item-add`/`item-edit`/`field-list`) have no REST
equivalent** — these remain GraphQL by necessity, not preference. Keep
board reconciliation batched and cached (`board_sync.py`'s delta-sync and
field-ID caching, hrse#386/harmonic-forge#203) rather than trying to
avoid GraphQL for these calls.

<!-- R-0020 -->
Rate-limit self-check: `gh api rate_limit --jq '.resources.graphql'`
before a batch of board-heavy operations. Prefer reading the
`X-Ratelimit-*` response headers on a real request over trusting a bare
`gh api /rate_limit` call in isolation — two live reads have been observed
to disagree in this environment; the headers on an actual request are the
more trustworthy source.
<!-- /R-0020 -->

Full investigation, live measurements, and the decomposed follow-up
issues (harmonic-forge#218 epic, #219–223): the research briefing at
`~/Harmonic_Projects/research/2026-08-10-gh-graphql-rate-limit-briefing.md`.

## CLOUD-NATIVE & 12-FACTOR READINESS

Target: every project survives a clean forklift to its cloud target.

<!-- R-0021 -->
- **Config:** zero hardcoded IPs, ports, URIs, keys, or model IDs — read from
  environment variables only. No `localhost` defaults baked into production
  paths.
<!-- /R-0021 -->
<!-- R-0022 -->
- **Statelessness:** don't write user uploads to local disk if the project is
  meant to run in a stateless/containerized environment — design for
  object storage instead.
<!-- /R-0022 -->
<!-- R-0023 -->
- **Observability:** no raw `print()`/`console.log()` debugging left in
  production paths — use the project's structured logger.
<!-- /R-0023 -->
<!-- R-0024 -->
- **Multi-tenancy:** never hardcode a user/tenant identifier; context flows
  from the authenticated session.
<!-- /R-0024 -->

## AI ABSTRACTION (LLM GATEWAY)

<!-- R-0025 -->
If a project makes LLM calls, all of them route through one designated
gateway module. No other module constructs a provider client directly or
hardcodes a model ID. Model/tier selection is env-first. LLM output is
untrusted input — parse and validate it, never `eval` it or raw-string-match
it for control flow.
<!-- /R-0025 -->

## LIVE VERSIONED VALUES (MODEL IDS, API VERSIONS, SDK SIGNATURES)

Any value that names a specific version of an external, actively-evolving
system — a model ID string, an API version, an SDK method signature/import
path — is not something training data can be trusted to produce correctly.
These strings look exactly as plausible when fabricated as when real, and
training data goes stale the moment a vendor ships a new version.

<!-- R-0026 -->
- If a handoff or fix requires such a value and it is not already present
  elsewhere in the codebase as a working reference, verify it live (vendor
  docs, WebFetch/WebSearch, or an existing skill's authoritative reference
  file) before writing it — never recall it from training.
<!-- /R-0026 -->
<!-- R-0027 -->
- Default to preserving the existing working value unless the task
  explicitly calls for a version change. A handoff that doesn't mention
  changing a model/version string is not license to "correct" it — that is
  scope creep on top of an unverified guess, the worst combination.
<!-- /R-0027 -->
<!-- R-0028 -->
- If verification isn't possible in the moment (no web access, no
  authoritative reference), say so explicitly and flag the value as
  unverified rather than silently substituting a plausible-looking guess.
<!-- /R-0028 -->

## API ABSTRACTION (FRONTEND)

<!-- R-0029 -->
If a project has a frontend that calls its own backend, all such calls route
through one designated HTTP client module. No raw `fetch()`/axios calls
scattered elsewhere. Components degrade gracefully (mock data / empty states)
when the backend is offline rather than crashing.
<!-- /R-0029 -->

## UTILITY / MIGRATION SCRIPTS

<!-- R-0030 -->
Check the project's designated scripts directory before writing a new
one-off. Never place single-use scripts inside the application source tree
(backend or frontend). Prefix single-use scripts so they sort together and
read as disposable (e.g. `1-fix_nodes.py`).
<!-- /R-0030 -->

## BUG-FIX PROTOCOL (ALL AGENTS)

<!-- R-0031 -->
Every bug fix — implementing or reviewing — follows read-propose-execute.
Skipping a step is a protocol violation.
<!-- /R-0031 -->

<!-- R-0032 -->
1. **Read before touching.** Locate and read the exact file sections involved
   before opening an editor.
<!-- /R-0032 -->
<!-- R-0033 -->
2. **Propose before executing.** State in plain text: the root cause (not
   just the symptom), the exact change (which lines, what replaces what), and
   the expected outcome (what log line / API response / state confirms it
   worked).
<!-- /R-0033 -->
<!-- R-0034 -->
3. **Execute, then verify against a stated test case.** Every bug-fix must
   include: "After fixing, [action] should produce [Y] and should NOT produce
   [Z]." Run that exact test against the live service — not just a re-read of
   the diff. See `rules/testing-gate.md` for what counts as live verification.
<!-- /R-0034 -->

<!-- R-0035 -->
If fixing multiple bugs, address each as a numbered item. Do not infer or
invent a bug that was not explicitly described.
<!-- /R-0035 -->

<!-- R-0036 -->
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
<!-- /R-0036 -->

<!-- R-0037 -->
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
<!-- /R-0037 -->

<!-- R-0038 -->
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
<!-- /R-0038 -->

## THE FILING BAR — THREE TESTS, IN ORDER (hrse, 2026-08-14)

<!-- R-0039 -->
**Applies before any agent, in any lane, creates a new issue.** It qualifies
every "file it" instruction in this file, including the section immediately
below, and supersedes any standing guidance that filing is default-on. This
bar governs **what** is filed; the lane sections above govern **who**
files — Lane 2 and Lane 3 surface a finding and stop, Lane 1 files.

Run these in order and stop at the first that answers:

1. **Does it block or corrupt the live pipeline the current release thesis
   names?** — File it.
2. **Has it actually bitten three or more times?** — File it. Cite the
   occurrences. Once or twice is a comment on the issue where it surfaced,
   not a new number.
3. **Otherwise** — fold it into the existing issue it touches, fix it inline
   if the fix is smaller than the issue would be, or let it go.
<!-- /R-0039 -->

Branch 3 is the one that gets skipped, and skipping it is what produced the
condition this rule exists to stop.

**Measured on `vitalharmony/hrse`, 2026-08-14** — the evidence, not a
hunch. Over three days: **60 issues created, 44 closed, net +16**. Standing
open: **205**, of which **75 (37%) were labelled `tech-debt` or
`infrastructure`** — work *about* the work, while the release thesis was
landing a role. Operator: *"every issue closed generates 3 more... I'll never
get anywhere at this rate."*

<!-- R-0040 -->
**A true observation is not sufficient grounds to file.** The failure mode is
not filing false things; it is filing true things nobody will ever action,
which is indistinguishable from noise once the list is long enough. An
unactioned issue is worse than an unwritten one, because it reads as signal
and costs re-triage every sweep.
<!-- /R-0040 -->

**Three consequences that are easy to get wrong:**

<!-- R-0041 -->
- **A once-only defect with an in-place guard does not earn an issue.** If the
  fix ships a comment at the site that stops the next person, the knowledge is
  durable already. File it if it recurs.
<!-- /R-0041 -->
<!-- R-0042 -->
- **Something smaller than its own issue should just be fixed.** A missing
  dependency in one checkout is an install, not a tracked work item. Tracking
  costs more than the fix.
<!-- /R-0042 -->
<!-- R-0043 -->
- **"It'll be lost otherwise" is answered by the issue it surfaced on**, not by
  a new one. The section below is right that a gate comment dies with its
  parent — so put it on the *parent that stays open*, or on the ADR, or in the
  rule file. Those are all durable and none of them add to the count.
<!-- /R-0043 -->

<!-- R-0044 -->
**When the bar says no but the finding is real, say so in one line and move
on.** Do not file it anyway "to be safe," and do not ask the operator to
adjudicate every observation — that just moves the triage cost rather than
removing it.
<!-- /R-0044 -->

## STANDING-RULE VIOLATIONS GET FILED, NOT FIXED, NOT JUST MENTIONED

**Gated by the filing bar above — run those three tests first.** This section
governs *how* a violation is recorded once it clears the bar, and is not
itself a licence to file. Most standing-rule violations found in passing will
land on branch 2 or 3.

<!-- R-0045 -->
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
<!-- /R-0045 -->

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

<!-- R-0046 -->
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
<!-- /R-0046 -->

<!-- R-0047 -->
Practical entry discipline: a memory entry states **FAILED / WHY / VERIFIED
/ RULE**. If a step wasn't done — the cause wasn't actually investigated, or
the fix wasn't verified live — say so, or don't store the entry as settled
fact. A guess marked as a guess is fine; a guess stored as a verified rule is
the failure mode this standard exists to prevent.
<!-- /R-0047 -->

This applies explicitly to every Lane 3 runtime's agent-maintained standing
instructions and memory surfaces — they need the same entry-quality bar to
avoid becoming a pile of unverified failure notes.

Does **not** apply to a project's `transaction-log.md` (or equivalent delta
log) — that is rung-1-by-design, a record of what changed, not a memory. The
ladder applies to whatever gets promoted *out of* a delta log into a real
rule, not to the log itself.

## SURGICAL CHANGES (KARPATHY PRINCIPLE)

<!-- R-0048 -->
Make the minimum change that accomplishes the stated task. Do not refactor,
rename, reorganize, or clean up adjacent code as part of a bug fix or small
feature — that is a separate task and a separate commit.
<!-- /R-0048 -->

<!-- R-0049 -->
- A bug fix touches only the lines needed to fix the root cause.
<!-- /R-0049 -->
<!-- R-0050 -->
- A small feature adds only what was asked for — no "while I'm here" cleanup.
<!-- /R-0050 -->
<!-- R-0051 -->
- Pre-existing style issues, unused imports, or long lines you didn't
  introduce: leave them, unless the task is explicitly "clean up X."
<!-- /R-0051 -->
<!-- R-0052 -->
- If decomposition is genuinely required to touch a file safely (it already
  exceeds the 300-line cap), say so explicitly before doing it, then do only
  that, then make the targeted change.
<!-- /R-0052 -->
<!-- R-0053 -->
- A comment that asserts a runtime fact (a measured cost, a live-confirmed
  count, "the board has N items") cites the test, issue, or measurement date
  that backs it (harmonic-forge#329) — an unattributed number reads as
  authoritative and silently goes stale the moment reality moves past it.
<!-- /R-0053 -->

## SHARED WORKING DIRECTORY — COMMIT BEFORE YOU YIELD

Worktree isolation (`3-lane-protocol.md`'s "Per-Lane Working Directories")
prevents *cross-lane* directory collisions, but each lane's own worktree
can still end up dirty at a bad moment (e.g. Lane 1 editing docs in the
main checkout while making an unrelated commit). The following discipline
applies **within each lane's own directory**, by **every** lane:

<!-- R-0054 -->
**Never yield control — post a completion comment, hand off, stop, or
otherwise signal "done" — while leaving uncommitted changes in the shared
directory.** Every stopping point ends in a clean `git status`. This
applies equally to:
<!-- /R-0054 -->

<!-- R-0055 -->
- **Lane 1** (Claude Code) — including doc-only commits (sprint-plan
  reconciliation, rules edits); never run a broad `git add` without
  reviewing the actual file list first, and never start editing without
  confirming the working tree is clean and it's your own change producing
  the diff.
<!-- /R-0055 -->
<!-- R-0056 -->
- **Lane 2** — a "no push requested yet" completion report
  must still mean the branch itself is fully committed; an in-progress
  follow-up fix (even a small one, even one Lane 1 or the operator asked
  for) gets its own commit before the session ends, not left staged or
  unstaged for someone else to find later.
<!-- /R-0056 -->
<!-- R-0057 -->
- **Lane 3** — beyond the existing never-fixes-anything rule
  (which already bars Lane 3 from *creating* uncommitted changes), Lane 3
  must not assume an uncommitted diff it finds mid-gate belongs to its own
  run. If Lane 3 discovers uncommitted changes when starting a gate, that
  is itself a stop-and-report condition (same fast-fail principle as an
  external blocker) — not something to puzzle out solo, work around, or
  silently gate against.
<!-- /R-0057 -->

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

<!-- R-0058 -->
**Every change to a tracked repo file happens in a dedicated per-issue
worktree and lands via a PR; work ends in the lane worktree, never
`/tmp`.** Never write while a checkout sits on `main` — branch or use a
worktree first, not after. Both halves are mechanism-backed where the
repo has it: a write outside the impl worktree while a lane is set is
denied at the tool-call level, and a stranded `/tmp` commit is detected
after the fact — where the repo doesn't have either yet, the discipline
above is what stands in.
<!-- /R-0058 -->

## ISSUE TRACKING

<!-- R-0059 -->
New bugs and features are tracked as GitHub Issues on the project's repo, not
appended to a local backlog file. Close issues directly on GitHub when work
completes and is verified.
<!-- /R-0059 -->
