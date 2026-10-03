# ADR-001: Adopt mise + process-compose instead of building a custom service manager ("paved")

**Date:** 2026-07-10
**Status:** Accepted — **Implemented, cutover complete 2026-07-13** (hrse#224
closed; `hrse_manager.py` and its five `manager_*.py` modules removed from
the HRSE2 repo; see hrse#237 for the final cutover)
**Decider:** Marc Mangus (platform owner)
**Supersedes:** the original scope of epic #34 and child issues #36–#39, #41–#43, #45–#46

> **Note, 2026-10-03 (harmonic-forge#883):** the transaction-log
> delta-summary pattern this ADR names (lines 15, 50 and 84) is superseded.
> The view is now rendered from git at read time and no file is committed;
> see `tools/transaction-log/README.md`. The text below is left as written.

## Context

HRSE2's `hrse_manager.py` (~1,360 lines across six modules) is the project's
sanctioned single entry point for service lifecycle: restart with
version-bump-and-commit, health-checked startup, a verification gate,
transaction-log delta summaries via a local LLM, GitHub Issue creation with
Projects-v2 board auto-add, podman-compose orchestration, and a
dev-environment dashboard. It encodes real, incident-tested lessons
(browser-safe port kills, cwd-sensitive mypy invocation, board-automation
unreliability workarounds, rootless-podman stats quirks).

Epic #34 proposed generalizing it into a public, pip-installable,
YAML-configured tool ("paved") shipped from this repo, decomposed into 11
child issues. Before starting implementation, the operator challenged the
premise: *"I'm worried I'm reinventing the wheel... I'm not in the tool
building business, I want to focus on CymaGraph, my IP."*

A live evaluation (2026-07-10) of the OSS landscape confirmed the concern.

## Decision

Do not build `paved`. Adopt existing OSS tools as the platform's golden
path, and publish **configs, glue scripts, and the workflow doc** — not a
bespoke binary:

| Need (former child issue) | Adopted answer |
|---|---|
| Config schema + loader (#36) | The tools' own configs: `process-compose.yaml`, `mise.toml` |
| Service lifecycle, health checks (#37) | **process-compose** — compose-style YAML for non-containerized processes; k8s-style readiness/liveness probes (`http_get`/`exec`); restart policies; dependency ordering; per-process working dirs and logs; TUI |
| Version bump + git commit (#38) | **Commitizen** (`cz bump`) or bump-my-version, wired as a mise task |
| Verification gate runner (#39) | **mise tasks** — named tasks with per-task `dir` (the cwd-sensitive-mypy lesson becomes one config line), dependencies, parallel execution |
| GitHub Issues + Projects v2 (#41) | **gh CLI built-ins** — `gh issue create` + `gh project item-add`; the custom GraphQL module in `manager_github.py` was already redundant |
| Container orchestration wrapper (#42) | podman-compose directly, aliased as a mise task |
| Dashboard (#43) | process-compose's TUI (processes + health); container stats glue retained only if still needed |
| Packaging/distribution (#45) | Disappears — nothing to package |
| HRSE2 migration (#46) | Re-filed as an epic on `vitalharmony/hrse`, where the implementation and Lane 3 testing actually happen |

What remains genuinely ours (not available off the shelf) stays as small
glue scripts published from this repo, invoked as mise tasks:

- The **transaction-log + LLM delta-summary** pattern (an agent context
  primer regenerated per commit) — tracked in #40, reshaped.
- The bump-and-commit-on-restart ceremony (a few-line task chaining
  existing tools).
- HRSE2's backlog.md sweep (project-specific; stays in HRSE2, not platform).

## Why this is the right call

1. **OSS Always.** The operator's standing priority order: pure OSS first,
   open-core fallback, commercial last. process-compose (Apache-2.0), mise
   (MIT), Commitizen (MIT), gh (MIT) are all pure OSS, actively maintained
   (mise ships releases every 1–2 weeks as of mid-2026).
2. **Focus.** Maintaining a public CLI tool — issues, docs, releases,
   compatibility — is a product commitment. The platform's product is the
   workflow; CymaGraph is the IP. Same reasoning that chose OpenTofu +
   Argo CD over custom build scripts (see the cloud-automation toolchain
   memo, 2026-07-10).
3. **Platform engineering, literally.** A paved road is conventions and
   configs on standard tools, not a bespoke binary. Publishing a
   `mise.toml` + `process-compose.yaml` template plus the incident-derived
   workflow doc shares the learnings *better* than a custom tool would —
   readers can adopt the pattern without adopting our code.
4. **Onboarding bonus.** mise also manages tool installation itself
   (pinned versions of node, python, process-compose, doctl, ...): one
   `mise install` in a fresh clone bootstraps a developer — the
   self-service story `harmonic-forge.md` §4 aims at.
5. **Better engineering than ours in places.** process-compose supervises
   its own child processes, which solves `hrse_manager.py`'s "browser
   safety" problem (kill only the listener on a port) more soundly than
   port-based kills.

## Consequences

- Epic #34 is reworked: children #36–#39, #41–#43, #45–#46 closed as
  superseded; #40 reshaped to "publish the transaction-log/delta-summary
  glue"; #34's remaining scope is the golden-path configs + workflow doc.
- A new epic on `vitalharmony/hrse` covers replacing `hrse_manager.py`
  with the adopted stack, gated by **test scripts proving feature parity**
  with every current `hrse_manager.py` capability before cutover (Lane 3
  live verification, per `rules/testing-gate.md`).
- `hrse_manager.py` remained the sanctioned tool for HRSE2 until that epic
  completed — no partial/ad-hoc migration. **Completed 2026-07-13**: mise +
  process-compose is now the sole service-lifecycle path for HRSE2.
- Risk accepted: two new third-party dependencies in the dev loop. Both
  are single static binaries, installable via mise itself, with large
  active communities; the exit cost if either dies is low (configs are
  simple and the pattern transfers).

## Sources (verified live, 2026-07-10)

- process-compose: github.com/F1bonacc1/process-compose;
  f1bonacc1.github.io/process-compose/health/ (probe spec)
- mise: github.com/jdx/mise; mise.jdx.dev/tasks/ (task runner, monorepo
  support since Oct 2025; v2026.5.x current)
- gh Projects v2 support: cli.github.com/manual/gh_project_item-add
- Commitizen bump: commitizen-tools.github.io/commitizen/commands/bump/
- Task-runner comparison (make/just/mise/go-task):
  mehdihadeli.com/blog/task-runners-comparison-2026
