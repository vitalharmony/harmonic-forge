# harmonic-forge

**The Vital Harmony AI Platform** — an Internal Developer Platform for AI-assisted engineering.

It defines one workflow, one rule set, and one toolchain that every project and
every AI agent at Vital Harmony operates from. Projects don't invent their own
process; they inherit this one and override only where they genuinely differ.

> *The platform IS the product.*

**Status:** Draft v0.2 — the protocol is in daily production use across two active
projects; distribution tooling and per-project rollout are still landing.
**License:** [MIT](LICENSE) · **Public by design** — see [Why this repo is public](#why-this-repo-is-public).

---

## The core idea

**The maker is never the grader.**

Work moves through three lanes with strict role separation. The agent that
writes the code never decides whether the code is good, and the agent that
grades it never gets to fix what it finds. Each lane runs in its own context,
its own working directory, and under its own directive file.

That constraint is the whole architecture. Everything else in this repo exists
to enforce it, distribute it, or keep it honest.

```
[GitHub Issue] ──▶ [ Lane 1 ] ──▶ [ Lane 2 ] ──▶ [ Lane 3 ] ──▶ [ PR ]
                   Blueprint      Muscle          Control Gate
                   diagnose       implement       verify, never fix
                   spec           to the spec     pass / fail
                        ▲                              │
                        └────────── FAIL ──────────────┘
```

| Lane | Role | Never does |
|---|---|---|
| **Lane 1 — Blueprint** | Diagnoses the problem, writes the implementation spec, posts a structured handoff | Never implements production code |
| **Lane 2 — Muscle** | Implements strictly against the handoff spec | Never invents scope, never runs the gate, never opens PRs |
| **Lane 3 — Control Gate** | Verifies against the spec with live evidence, returns PASS or FAIL | **Never fixes anything** — a failing test is the protocol working |

Lanes are defined by *role*, not by vendor. The reference implementations have
been Claude Code, Devin, and Codex in various combinations; a lane's tooling is
swappable, its constraints are not. The `LANE` environment variable, set once at
session launch by the `lane1`/`lane2`/`lane3` launchers, is what the enforcement
hooks actually key on.

---

## Capabilities

### Rules that travel
Seven universal directive files in `rules/` — role directives for each lane,
language guardrails for Python and TypeScript, and the Lane 3 gate spec with its
pass/fail thresholds. `sync_rules.py` symlinks them into every project, so a rule
fixed once is fixed everywhere on the next pull.

**Override precedence** (lowest → highest):
```
harmonic-forge/rules/universal-*.md
  → harmonic-forge/rules/{language}.md
    → {project}/.windsurfrules
      → {project}/CLAUDE.md
```

### Agents that are project-agnostic
Four universal subagents in `agents/`, distributed to every project automatically:

- **`sticky-wicket`** — a circuit breaker. Two consecutive gate failures on one
  issue means the approach may be wrong, not the latest patch. Reads the thread
  cold and asks whether to reforge rather than patch again.
- **`pitch-inspection`** — a pre-flight second read on a Lane 1 handoff *before*
  it ships, triggered by checkable conditions (design alternatives existed,
  assumptions are asserted rather than verified, the work mutates live state).
- **`product-strategy`** — high-judgment architecture and positioning calls,
  parameterized by project rather than hardcoded to one.
- **`ai-review-queue-synthesis`** — batch-synthesizes external research briefs
  into one deduplicated, live-verified backlog proposal. Advisory only: it
  surveys read-only and proposes, and never touches an issue.

### Skills that are opt-in
`skills/` holds distributable Claude Code skill directories. Unlike agents,
**nothing here distributes just by existing** — a skill's description is
surfaced and directly invocable the moment it's linked, so projects opt in
explicitly via `UNIVERSAL_SKILL_DIRS` or `--skill`.

### Enforcement that isn't prose
Prose rules proved insufficient on their own, repeatedly, so the constraints
that kept getting violated became executable hooks in `tools/hooks/`:

| Hook | Stops |
|---|---|
| `block_lane1_status_claims.py` | Lane 1 posting unverified status, or a Lane 2 session writing into the main checkout |
| `deny_lane3_ae_self_post.py` | Lane 3 authorizing its own approval |
| `model_tier_gate.py` | High-estimate issues entering a lane on an under-powered model |
| `mypy_cwd_trap.py` | A known mechanical footgun that produced phantom type errors |
| `remind_gate_readiness_sweep.py` | Gate-readiness claims that skipped the sweep |
| `check_worktree_busy.py` | A checkout yanking state out from under a live process in a sibling worktree |

### Shared tooling
- **`tools/gh/`** — repo-agnostic GitHub helpers. `gh_issue.py` (create + board
  placement), `post_comment.py` (self-checking `--body-file` posts), a call
  cache, and a closing-keyword guard. `--repo` is mandatory with no default,
  because a default once caused a mis-post.
- **`tools/transaction-log/`** — project-agnostic per-commit delta log and
  diffstat glue, so a session can see what other sessions did since the last
  version bump.
- **`tools/lane/`** — the `lane1`/`lane2`/`lane3` launchers that put a session in
  the right worktree with the right role signal.
- **`templates/golden-path/`** — a reference `mise.toml` and `process-compose.yaml`
  for a project's service lifecycle, so every project starts and restarts the
  same way.

---

## Architecture

```
harmonic-forge/
├── harmonic-forge.md          # full platform specification (start here)
├── 3-lane-protocol.md         # condensed, agent-readable operational protocol
├── sync_rules.py              # bootstrapper: clone + symlink into a project
├── mise.toml                  # this repo's own tasks
├── rules/                     # 7 universal directive files
├── agents/                    # 4 project-agnostic subagents
├── skills/                    # opt-in distributable skills
├── templates/                 # handoff, HITL review, golden path
├── tools/                     # gh, hooks, lane launchers, transaction log, worktree
├── docs/decisions/            # platform ADRs
└── .githooks/pre-commit       # rejects direct commits to main
```

Each project keeps its own sovereignty and layers on top:

```
{project}/
├── CLAUDE.md                  # project context + pointer back here
├── .windsurfrules             # project-specific overrides
└── .claude/
    ├── rules/   → symlinks into harmonic-forge/rules/
    ├── agents/  → symlinks into harmonic-forge/agents/   (all, automatically)
    └── skills/  → symlinks into harmonic-forge/skills/   (opt-in only)
```

**Two structural decisions worth knowing:**

**Handoffs are permanent.** The prevailing public pattern treats an inter-agent
handoff as disposable — written to a temp directory, never committed. This
platform inverts that deliberately: a Lane 1 handoff is a permanent comment on
the GitHub issue it belongs to. Same handoff discipline, different retention
decision, because the audience here is cross-issue pattern analysis and an
auditable trail of why each change was made — not one developer's single session.

**Every lane gets its own working directory.** Lanes run in separate `git
worktree`s, and implementation work goes in a fresh per-issue worktree rather
than any shared checkout. This is not a style preference: concurrent checkouts
in a shared directory silently destroyed in-flight work often enough that the
worktree rule is now enforced by tooling.

---

## How a change actually moves

1. **An issue exists.** Nothing enters a lane without one. It carries a point
   estimate, and 13+ points triggers a decomposition check before any lane starts.
2. **Lane 1 diagnoses and specs.** It reads the code, finds the root cause, and
   posts a structured handoff to the issue — root cause, explicit spec, test
   cases, load-bearing assumptions marked verified or asserted. If the design had
   real alternatives or rests on unverified assumptions, `pitch-inspection` reads
   the draft first.
3. **HITL gate.** A human approves the handoff before credits burn. For larger
   work Lane 2 posts its *plan* first and gets that reviewed before implementing.
4. **Lane 2 implements** against the spec, in its own worktree, and posts a diff
   summary back to the issue. It does not push, open PRs, or file issues.
5. **Lane 3 verifies** with live evidence — real requests, real logs, real counts,
   not a re-reading of the source — and returns PASS or FAIL. On FAIL it reports
   and stops. It never fixes.
6. **Two consecutive FAILs** on one issue triggers `sticky-wicket`, on the theory
   that repeated incremental fixes that aren't converging mean the approach is
   wrong.
7. **PR, human merge.** Only the platform owner merges into `harmonic-forge`.

**The Tooling Exception:** dev and test tooling changes skip the full loop. The
gate exists to protect production behavior, and applying it to a script that only
developers run costs more than it protects. The exception is explicit, scoped,
and documented in `3-lane-protocol.md` — not an ad-hoc bypass.

---

## Quickstart

```bash
# 1. Clone the platform (public, no credentials needed)
git clone https://github.com/vitalharmony/harmonic-forge.git ~/harmonic-forge

# 2. Wire a project into it
cd /path/to/your-project
python3 ~/harmonic-forge/sync_rules.py --project .

# 3. Opt into a platform skill
python3 ~/harmonic-forge/sync_rules.py --project . --skill impl-worktree

# 4. Refresh when platform rules change
python3 ~/harmonic-forge/sync_rules.py --pull
```

Then read [`3-lane-protocol.md`](3-lane-protocol.md) before pulling a first ticket.

**Working on this repo itself:**
```bash
git config core.hooksPath .githooks   # required — direct commits to main are rejected
mise run check                        # verification gate
mise run commit                       # stage + transaction-log entry + commit, in that order
```

macOS and Linux. Symlinks work natively on both; Windows needs WSL.

---

## Why this repo is public

Every project's GitHub access is fully isolated — no shared identity, no
machine-global token reaching across client boundaries, each project's tooling
resolving credentials from that project's own gitignored env file.

`harmonic-forge` being public is the exception that makes that work cleanly: it
is the one thing every project reads without needing any credential at all.

---

## Documentation

| Document | What it's for |
|---|---|
| [`harmonic-forge.md`](harmonic-forge.md) | Full platform specification — philosophy, structure, topology, registry |
| [`3-lane-protocol.md`](3-lane-protocol.md) | Condensed, agent-readable operational protocol |
| [`rules/`](rules/) | The universal directive files themselves |
| [`docs/decisions/`](docs/decisions/) | Platform ADRs |
| [`templates/lane1-handoff.md`](templates/lane1-handoff.md) | The handoff format |
| [`tools/gh/README.md`](tools/gh/README.md) | Shared GitHub tooling and its guardrails |

---

## Provenance

This platform was built primarily from its own operational failures. Most rules
here exist because something broke, was diagnosed, and the diagnosis was distilled
into a rule — the incident record in the project issue trackers is the primary
source, and the rules are downstream of it.

Where external work shaped the design, it is credited here.

**[Platform Engineering](https://platformengineering.org)** — the framing this
repo is organized around: golden paths that make the right way the easy way,
self-service onboarding from a single bootstrapper, and treating the platform as
a versioned product with its own backlog rather than a pile of shared config.

**Lance Martin (Anthropic), "Designing loops with Fable 5" (June 2026)** — two
distinct contributions:

- *External validation of the lane separation.* The maker-never-grader principle
  was arrived at independently here, from real incidents, before any external
  corroboration existed. Martin's continual-learning benchmark then quantified
  the same structure: an independent verifier in its own context window, seeing
  only the artifact and the rubric, verified up to **73%** of answers, versus
  **7–33%** when a model critiqued its own work in its own context. Anthropic's
  framing in the same material — *"tuning a standalone evaluator to be skeptical
  is far more tractable than making a generator critical of its own work"* —
  matches the Lane 3 design directly.
- *The five-rung memory ladder*, adopted as the explicit quality bar for every
  durable memory entry on the platform: **fail → investigate → verify → distill
  → consult.** A memory that stops at "fail" is a diary entry; one that reaches
  "distill" is a rule the next session can act on.

**Anthropic, "Loop engineering: Getting started with loops"** — independently
recommends the same shape for code review: a second agent, in fresh context.

**Matt Pocock's [`/handoff` skill](https://github.com/mattpocock/skills)** (MIT) —
the most widely adopted public treatment of inter-agent handoffs. The Lane 1
handoff format was compared against it directly, and three deltas were adopted:
a gate-variant field, an explicit no-secrets line, and a considered decision on
pointers versus verbatim duplication. The formats agree on fundamentals; where
they diverge, the divergence is now deliberate rather than accidental — see the
retention decision under *Architecture* above.

**The AI Review Queue.** External research reaches this platform through a
standing pipeline rather than ad hoc: analysis briefs are batch-synthesized by
the `ai-review-queue-synthesis` agent into a single deduplicated recommendation
set, every repo-state claim is verified live against the actual checkouts and
backlog, and the output is a filing proposal a human acts on. Ideas become issues
only after that verification step, which is why the provenance above is short and
specific rather than a reading list.

---

© 2026 Vital Harmony LLC · Released under the [MIT License](LICENSE).
