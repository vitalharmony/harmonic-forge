# Vital Harmony AI Platform — 3-Lane Protocol Specification

**Owner:** Marc Mangus / Vital Harmony LLC
**Status:** Draft v0.2 — core protocol content finalized; tooling (`sync_rules.py`) and per-project rollout in progress
**Applies to:** All Vital Harmony projects (CymaGraph/HRSE2, Ke'nekted, LeasePAL, OWE Studio)
**Platform repo:** https://github.com/vitalharmony/harmonic-forge (public)

---

## 1. Philosophy

> *The platform IS the product.*

This document specifies the Vital Harmony AI Development Platform — an
Internal Developer Platform (IDP) for AI-assisted engineering. It defines
the universal workflow, toolchain, governance rules, and golden paths every
developer and AI agent on every project operates from.

Grounded in [Platform Engineering](https://platformengineering.org)
principles:
- **Golden paths** make the right way the easy way — a paved road, not a wall.
- **Self-service** — any developer onboards from a single bootstrapper script.
- **Platform as product** — this repo is versioned, maintained, and treated
  as a first-class product with its own issues and changelog.
- **Paved roads, not walls** — the HITL gate and test guardrails exist to
  accelerate, not block.

---

## 2. The 3-Lane Assembly Line

Every developer on every project executes this loop locally before pushing a
PR. Full agent-readable version: `3-lane-protocol.md`.

```
[GitHub Issue] ──> Dev Pulls Ticket ──> [Local 3-Lane Loop] ──> GitHub PR
                                          │
                                    Lane 1: Claude Code (Blueprint)
                                    Lane 2: Devin Local (Muscle)
                                    Lane 3: Devin AA (Control Gate) — local
```

**Lane 3 is local by design.** The Devin Autonomous Agent runs on the
developer's own machine, with local `gh` CLI access to GitHub — there is no
separate cloud-dispatched agent in this architecture. This matters for the
gate design: Lane 3 needs the same local repo/issue access as Lanes 1 and 2,
nothing more exotic.

See `rules/universal-agent.md`, `rules/universal-lane1.md`, and
`rules/universal-claude.md` for the full directive set each lane operates
under, and `rules/testing-gate.md` / `rules/frontend-ui-golden-path.md`
for Lane 3's pass/fail thresholds.

**Lane 3 also has its own local directives file**, outside this repo and not
synced by `sync_rules.py`: `~/.config/devin/AGENTS.md` (machine-specific,
not per-project). Direct corrections to Lane 3's own behavior go there;
Devin AA also writes to it itself. See `3-lane-protocol.md`'s Lane 3 section
for the full note.

**Lane 3 also has a per-project skill file** (distinct from `AGENTS.md`
above — repo-local, not machine-wide): `{project}/.devin/skills/lane3-gate/
SKILL.md`. Hard-wired enforcement for the never-fixes-anything constraint,
authored by Devin AA after prose rules alone proved insufficient (real
incidents: HRSE2 `#176`, `#52`). Explicitly defers to `3-lane-protocol.md`/
`testing-gate.md` as authoritative. Currently HRSE2-only, not yet part of
`sync_rules.py`'s distribution — see `3-lane-protocol.md`'s Lane 3 section.

**Handoffs are permanent, by deliberate choice.** The prevailing public
pattern for inter-agent handoffs (e.g. Matt Pocock's widely-adopted
`/handoff` skill) treats them as disposable — written to a temp directory,
never committed, optimized for one developer's single-session ergonomics.
This platform inverts that on purpose: a Lane 1 handoff is posted as a
permanent comment on the GitHub issue it belongs to, because the audience
here is different — cross-issue pattern analysis across the 3-lane loop,
and a diligence/investor trail showing why each change was made. Same
underlying handoff discipline (root cause, explicit spec, test cases), a
different retention decision because the platform optimizes for the
auditable record over single-session disposability. See
`templates/lane1-handoff.md`.

---

## 3. Repository Structure

### Platform Repo (`vitalharmony/harmonic-forge`, public)

```
harmonic-forge/
├── harmonic-forge.md                  # this document
├── README.md                       # repo landing page
├── 3-lane-protocol.md              # condensed operational protocol (agent-readable)
├── mise.toml                       # this repo's OWN tooling (harmonic-forge#54) — check/commit/
│                                    # gh-new-issue/post-comment only, deliberately no
│                                    # bump/restart (no running artifact to stamp — see
│                                    # templates/golden-path/README.md's "docs/tooling-repo
│                                    # subset" section for the full reasoning)
├── scripts/
│   └── git_commit.py               # this repo's own commit task — calls into
│                                    # tools/transaction-log/ directly, doesn't reimplement it
├── transaction-log.md              # this repo's own transaction log — cleared on push to
│                                    # main (no version bump exists to hang rotation on)
├── .githooks/
│   └── pre-commit                  # rejects direct commits to main (harmonic-forge#52); every
│                                    # clone must run `git config core.hooksPath .githooks`
├── .gitignore
├── prompts/                        # Gemini Gem / external-tool prompt templates
├── rules/
│   ├── universal-lane1.md          # role-general Lane 1 directives (all projects)
│   ├── universal-claude.md         # Claude-Code-CLI mechanics for Lane 1 (all projects)
│   ├── universal-agent.md          # universal .windsurfrules directives (all projects)
│   ├── backend-python.md           # universal backend guardrails
│   ├── frontend-typescript.md      # universal frontend guardrails
│   ├── testing-gate.md             # Lane 3 gate spec + coverage thresholds
│   └── frontend-ui-golden-path.md  # Greg's variant — visual regression + smoke
├── templates/
│   ├── lane1-handoff.md            # Lane 1 → 2 structured handoff template
│   ├── hitl-test-review.md         # Tech Lead HITL approval checklist
│   └── golden-path/                # reference mise.toml + process-compose.yaml +
│                                    # workflow doc for a project's service-lifecycle path
├── tools/
│   ├── transaction-log/            # published, project-agnostic transaction-log +
│   │                                # diffstat glue (library + CLI)
│   └── gh/                         # published, repo-agnostic GitHub helpers (harmonic-forge#53)
│                                    # — post_comment.py (self-checking --body-file post) +
│                                    # gh_issue.py (create + optional board-add); --repo is
│                                    # required with no default on both (see README.md there
│                                    # for the mis-post incident that caused this)
├── agents/                         # universal, project-agnostic subagents only —
│   │                                # see the note below before adding one
│   ├── sticky-wicket.md            # reactive cross-lane thrashing circuit breaker (Fable)
│   ├── pitch-inspection.md         # proactive pre-flight handoff second read (Fable)
│   ├── product-strategy.md         # high-judgment product/architecture strategy calls (Fable) —
│   │                                # {project}-parameterized, invoking session supplies context
│   └── ai-review-queue-synthesis.md # batch synthesis of a Drive review-queue of video briefs
│                                    # into one prioritized doc ending in a GitHub filing plan
│                                    # (Opus) — target-parameterized (queue folder, repos,
│                                    # exemplar supplied by the invoking session); read-only
│                                    # on GitHub, writes only to Drive (harmonic-forge#224)
├── skills/                          # distributable Claude Code skills (harmonic-forge#169) —
│   │                                # directories (<name>/SKILL.md, + optional sibling
│   │                                # files/dirs), symlinked whole, opt-in only via
│   │                                # UNIVERSAL_SKILL_DIRS or --skill — unlike agents/,
│   │                                # nothing here distributes just by existing (a skill's
│   │                                # description is surfaced and directly invocable the
│   │                                # moment it's linked, with no activation gate the way
│   │                                # a vertical package has)
│   └── _stub/                      # proof-of-mechanism only, never opted in by default
│       ├── SKILL.md
│       └── agents/openai.yaml      # proves multi-file skill dirs survive the symlink
├── docs/
│   ├── decisions/                  # platform ADRs (ADR-NNN-*.md, sequential)
│   ├── operator-diffs.md           # HITL-facing "what's different from the norm" notes
│   └── onboarding-*.md             # per-developer onboarding addenda
└── sync_rules.py                   # bootstrapper — clone + symlink setup for
                                     # rules/ (UNIVERSAL_RULE_FILES list), agents/
                                     # (auto-discovered, every *.md in agents/), and
                                     # skills/ (UNIVERSAL_SKILL_DIRS list + --skill,
                                     # opt-in only) into {project}/.claude/rules/,
                                     # .claude/agents/, and .claude/skills/
```

**`agents/` note:** only agents written project-agnostically (no hardcoded
project name, no project-specific domain rules baked in) belong here.
`product-strategy.md` was originally HRSE2-local (built 2026-07-10, before
this canonical pattern existed) with CymaGraph specifics hardcoded
throughout (explicit "CymaGraph (HRSE2)" framing, a sovereignty/privacy
filter tied to CymaGraph's own BYOC/BYOK positioning). Generalized
2026-07-13 (harmonic-forge#48): the project name and its positioning/ethical
commitments are now supplied by the invoking session's prompt instead of
hardcoded — the agent reads whatever the project has actually committed to
(its own README/ADRs) rather than assuming CymaGraph's stance applies
everywhere. When invoking it, name the project explicitly and point at
where its positioning commitments (if any) are documented.

### Per-Project Structure (each sovereign repo)

```
{project}/
├── CLAUDE.md                       # project-specific overrides + pointer to harmonic-forge
├── .windsurfrules                  # project-specific overrides
├── .claude/rules/                  # path-scoped rules (fire on matching files)
│   ├── backend-python.md           # → symlink to harmonic-forge/rules/backend-python.md
│   └── frontend-typescript.md      # → symlink to harmonic-forge/rules/frontend-typescript.md
├── .claude/agents/                 # universal subagents, symlinked by sync_rules.py
│   ├── sticky-wicket.md            # → harmonic-forge/agents/sticky-wicket.md
│   └── pitch-inspection.md         # → harmonic-forge/agents/pitch-inspection.md
├── .claude/skills/                 # opted-in platform skills, symlinked by sync_rules.py
│   │                                # (empty unless a project explicitly opts a skill in —
│   │                                # see harmonic-forge#169; UNIVERSAL_SKILL_DIRS is
│   │                                # empty by default)
│   └── impl-worktree/              # → harmonic-forge/skills/impl-worktree (harmonic-forge#207,
│                                    # once it lands — the first real opted-in entry)
└── (project may keep additional local-only rule/agent/skill files, e.g. a
    Cypher/Neo4j addendum or a project-specific strategy subagent, that are
    NOT part of the platform sync because they don't apply universally)
```

**Override precedence (lowest → highest):**
```
harmonic-forge/rules/universal-*.md
  → harmonic-forge/rules/{language}.md
    → {project}/.windsurfrules (project overrides)
      → {project}/CLAUDE.md (session-specific context)
```

---

## 4. `sync_rules.py` Bootstrapper

**Purpose:** any developer on any project runs this once to wire their local
machine into the platform, and again whenever platform rules change.

**What it does:**
1. Clones (or pulls) `vitalharmony/harmonic-forge` into `~/harmonic-forge/`.
2. Creates symlinks from the project's `.claude/rules/` to the platform's
   universal rule files, **and** from `.claude/agents/` to every
   auto-discovered agent in `harmonic-forge/agents/` (no separate list to
   maintain — anything placed in `agents/` is definitionally universal,
   see the `agents/` note above).
3. Symlinks whole skill directories (`harmonic-forge#169`) from
   `harmonic-forge/skills/<name>/` into the project's `.claude/skills/<name>/`
   — but only for names in `UNIVERSAL_SKILL_DIRS` (empty by default) or passed
   explicitly via a repeatable `--skill <name>` flag. Unlike `agents/`, nothing
   in `skills/` distributes just by existing — a skill's `description` is
   surfaced in every session's skill listing and is directly invocable the
   moment it's linked, with no activation gate the way a vertical package has.
4. Verifies symlink integrity (rules, agents, and any opted-in skills) and
   reports any broken links.
5. Prints a checklist of manual steps remaining (e.g. project-level
   `CLAUDE.md` setup).

**Usage:**
```bash
# One-time setup (from any project root)
python3 ~/harmonic-forge/sync_rules.py --project .

# Opt into a specific platform skill too
python3 ~/harmonic-forge/sync_rules.py --project . --skill impl-worktree

# Pull latest platform rules (run at session start or after platform updates)
python3 ~/harmonic-forge/sync_rules.py --pull
```

**Platform:** macOS and Linux. Symlinks work natively on both. Windows is
not currently supported — use WSL if needed.

**Branch protection (required, not automated by the bootstrapper):** this
repo has a `.githooks/pre-commit` hook (added via harmonic-forge#52) that
rejects any commit made while on `main`. It is **not wired up by default**
— every clone must run this once:
```bash
git config core.hooksPath .githooks
```
Without it, nothing stops a direct commit to `main` (this bit HRSE2 on
#236, then this repo itself before the hook existed — see ADR-005).

---

## 5. Team Topology

| Role | Person(s) | Responsibility |
|---|---|---|
| Platform owner | Marc | Owns `harmonic-forge`; sets golden paths; reviews platform PRs |
| Feature delivery | Kyle (CymaGraph/HRSE2), Greg (Ke'nekted) | Consume golden paths; deliver against backlog; never modify platform rules directly |
| Product demand | Shawn | Defines acceptance criteria on issues; approves shipped features |

**Platform change policy:** only the Platform Team (Marc) merges PRs into
`harmonic-forge`. Stream-aligned developers open issues or PRs against
`harmonic-forge` to propose changes — they never push directly.

**Ajit / multi-agent extension:** a possible future Lane 3 plugin interface
for a custom multi-agent setup is tracked separately (issue #8) and is
explicitly parked — involvement and framework are unconfirmed. Nothing in
the core protocol depends on it.

---

## 6. Onboarding a New Developer

1. `harmonic-forge` is public — clone it directly, no invite needed:
   `git clone https://github.com/vitalharmony/harmonic-forge.git ~/harmonic-forge/`.
2. Get collaborator access to your own project's repo (this is per-project,
   not per-platform — see §7 and the security model below).
3. Clone the project repo and run: `python3 ~/harmonic-forge/sync_rules.py --project .`
   (one-time; a project with `.githooks/post-checkout`/`post-merge` wired,
   e.g. HRSE2, harmonic-forge#209, keeps this current automatically after
   the first run — every later checkout/merge re-syncs without a manual
   step).
4. Follow the project's `setup/first_time_setup.md` for environment setup.
5. Read `3-lane-protocol.md` before pulling a first ticket.
6. Pull a `tech-debt`-labeled issue as a first ticket (lowest stakes, real
   codebase exposure).

**Security model — credential isolation across projects:** each project's
GitHub access is fully separate. There is no shared identity across
projects — `vitalharmony`-authenticated tooling never touches
`kenekted-platform` (or any other client repo), and vice versa. Project-level
tools (Lane 2/3 agents, `gh` CLI) resolve their GitHub token from that
project's own gitignored env file, never from a different project's token or
a machine-global `gh auth login` session. `harmonic-forge` being public is the
exception that makes this work cleanly — it's the one thing every project
reads without needing any credential at all.

See `docs/onboarding-kyle.md` and `docs/onboarding-greg.md` for
project-specific addenda.

---

## 7. Project Registry

| Project | Repo | Status | Primary Stack |
|---|---|---|---|
| CymaGraph (HRSE2) | vitalharmony/hrse | Active — 3-lane operational | FastAPI + Neo4j + React 19 |
| Ke'nekted | harmonicarchitect/kenekted-platform (private, `marc@kenekted.ai`) | Active | TBD |
| LeasePAL | TBD | Active | TBD |
| OWE Studio | TBD | Active | TBD |

**Cross-project sequencing:** `docs/PRIORITIES.md` in the `hrse` repo is the
canonical "what gets worked on next" doc — it deliberately spans both
`vitalharmony/hrse` and `vitalharmony/harmonic-forge` issues (Marc operates
across both from the same working session, so a hrse-only or
harmonic-forge-only priority list would miss real interdependencies). Kept in
sync by HRSE2's `/sprint-plan` skill, which as of 2026-07-13 also mirrors
it onto matching `Priority`/`Sequence` fields on two GitHub Projects v2
boards (vitalharmony project #1 for hrse, #3 for harmonic-forge) — the doc
stays canonical (it carries the reasoning, the boards don't), the boards
are derived views for anyone who prefers glancing at a kanban layout. As
more projects come online, this may need to move to a platform-level
location — not done yet since it's only ever covered two repos so far.

---

## 8. Open Items (pending before v1.0)

- [x] Write `3-lane-protocol.md` (condensed agent-readable version of this doc)
- [x] Write all `rules/` files
- [x] Write both `templates/` files
- [x] Implement `sync_rules.py`
- [x] Migrate HRSE2 `.windsurfrules` and `CLAUDE.md` to local-override pattern
- [ ] Add `sync_rules.py` step to HRSE2 `setup/first_time_setup.md`
- [x] Write Kyle-specific and Greg-specific onboarding addenda (Greg's stays
      a placeholder pending the still-TBD registry entries above)
- [ ] Confirm OS for Greg and Kyle → validate symlink path assumptions
- [ ] Define Ke'nekted, LeasePAL, OWE Studio project-level override structure
      (repo location and stack are still TBD in the registry above)
- [ ] Confirm Ajit's involvement/framework before touching issue #8 — parked,
      not blocking v1.0

---

## 9. References — External Validation

The lane separation in §2 — the maker is never the grader — was arrived at
independently, from real incidents on this platform (HRSE2 `#149`, `#176`,
`#186`), before any external corroboration existed. It now has one: Anthropic
has published quantified findings that match the platform's core structural
principle.

**Lance Martin (Anthropic), "Designing loops with Fable 5" (June 2026):**
in a continual-learning benchmark, an independent verifier — running in its
own context window, seeing only the artifact and the rubric, not the maker's
reasoning trail — verified up to **73% of answers** (Fable 5, best runs),
versus **7–33%** (median ~17%, Opus 4.7) when a model critiqued its own work
in its own context. Anthropic's own framing, quoted in the same material:
*"Tuning a standalone evaluator to be skeptical is far more tractable than
making a generator critical of its own work."* Agents grading their own work
tend to confidently praise it even when the quality is mediocre.

Anthropic's companion engineering post, **"Loop engineering: Getting started
with loops"** (claude.com/blog/getting-started-with-loops), independently
recommends the same shape: a second agent, in fresh context, for code review.

**What this changes:** nothing operative. This section is documentation
lineage only — `3-lane-protocol.md` remains the no-narrative, agent-loaded
source of truth, and the internal incidents that produced each rule remain
the primary record. This section exists because a future diligence reader
(or a new team member) benefits from knowing the architecture wasn't just
internally reasoned — it matches published, quantified, external guidance
that arrived after the platform's own design was already in production.
