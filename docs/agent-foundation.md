# Agent Foundation — harmonic-forge

Vendor-neutral directives for any agent (Claude Code, Codex, Gemini CLI, or
a human) working **inside this repo**. `CLAUDE.md`, `AGENTS.md` and
`GEMINI.md` at the repo root are thin entrypoints that load this file; the
content lives here once (harmonic-forge#321).

## What this repo is

`harmonic-forge` is the **Vital Harmony AI Platform** — the Internal
Developer Platform every other Vital Harmony project inherits its workflow,
rules, and toolchain from. It ships no application code and serves no users.
Its work product is the protocol, the rule corpus, and the tooling that
distributes and enforces them.

> *The platform IS the product.*

## The case this file exists for

Editing the platform **from inside the platform** is the one case with no
other coverage. A session started in a *project* checkout gets the platform
rules symlinked in by `sync_rules.py`; a session started here does not — it
is editing the source those symlinks point at. Before harmonic-forge#321
that session had no directive entrypoint at all.

## Read first

| File | What it holds |
|---|---|
| `3-lane-protocol.md` | The agent-readable protocol — lane roles, launchers, Tooling Exception, escalation. The operative document. |
| `harmonic-forge.md` | The human-facing specification and philosophy behind it. |
| `rules/universal-agent.md` | Universal agent rules — the filing bar, standing-rule violations, modularity. |
| `rules/universal-lane1.md` | Lane 1 role requirements and the handoff format. |
| `rules/universal-claude.md` | Claude-Code-CLI mechanics specifically. |
| `rules/testing-gate.md` | What a verification claim has to be backed by. |
| `rules/lane-shorthand.md` | `L2P`/`L3F`/`H<N>`/`F<N>` shorthand, and the `BATCH` keyword. |

`transaction-log.md` holds per-commit deltas since the last version bump —
read it for what other sessions have done that is not yet reflected
elsewhere.

## Conventions specific to this repo

**No release milestones — by rule, not by oversight.** `harmonic-forge`'s
work is pulled by multiple ventures and belongs to no single venture's
release, so it carries no milestone field. `hrse` requires `--milestone` on
every new issue; **do not infer that convention applies here, and do not
"fix" the gap.** Full rule: HRSE2's `docs/ROADMAP.md`.

**Board:** Projects v2 board #3, "ai-platform Backlog", owner `vitalharmony`.
`tools/gh/gh_issue.py` needs `--project-owner`/`--project-number` passed
explicitly for this repo. Required fields on every open issue: `Tier`
(`fast`/`standard`/`deep` — a model-routing signal, not a forecast),
`Theme`, and `Venture`. `Venture: Platform` here means *genuinely shared* —
forge work driven by a named product carries that product's venture instead.

**Issue shorthand:** `F<N>` refers to a `harmonic-forge` issue, `H<N>` to an
`hrse` one. Cross-repo references must be written `vitalharmony/hrse#N` —
`hrse#N` alone does not autolink and fails silently.

**Lane sessions start through the launchers**, never a bare CLI invocation:
`lane1`/`lane2`/`lane3` (`tools/lane/`, on `$PATH`). They set `LANE`, `cd`
into the correct worktree, and build the per-CLI launch command via
`tools/lane/_cli_launch.sh`. Select the agent with `LANE_CLI`
(`claude` default, `codex`, `gemini`).

**The maker is never the grader.** This holds even inside a single lane:
under the Tooling Exception a single implementer writes the tooling in one
pass, but never grades its own verification — get a fresh-context second
read before trusting a result. See `3-lane-protocol.md` § Tooling Exception
and `docs/decisions/ADR-002-*`.

**Changes here propagate.** A rule file edited in this repo reaches every
project through `sync_rules.py`. Treat a rules edit as a change to every
project at once, not a local one.
