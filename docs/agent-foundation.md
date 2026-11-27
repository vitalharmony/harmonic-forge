# Agent Foundation — harmonic-forge

Vendor-neutral directives for any agent (Claude Code, Codex, or a human)
working **inside this repo**. `CLAUDE.md` and `AGENTS.md` at the repo root
are thin entrypoints that load this file; the
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
| `rules/lane-shorthand.md` | Lane shorthand (`L2S`/`L3F`/`H<N>`/`F<N>`), `BATCH`, and EOQ for chat and belt events. |
| `rules/lane-tooling-reference.md` | Reference for `lane_state.py`'s derived states, moved out of `lane-shorthand.md` (harmonic-forge#909). Linked into no session. |

The transaction-log view — recent commits on HEAD, rendered from git — is
injected at session start; `mise run transaction-log` prints it on demand.
Read it for what other sessions have done that is not yet reflected
elsewhere. There is no `transaction-log.md` (harmonic-forge#883).

## Conventions specific to this repo

**Its own release series.** `harmonic-forge`'s work is pulled by multiple
ventures and belongs to no single venture's release, so it carries its own
SemVer `0.y` milestones plus `Later`, independent of CymaGraph's series
(operator decision 184, harmonic-forge#1382). Once those milestones exist,
`--milestone` is required on every new issue, as in `hrse`. Filing into
`Later` (any open milestone that is not the current release) owes no handoff
and requires `--deferral-trigger` and `--deferral-owner`, posted as the R-0352
deferral comment. Release theses: [`docs/ROADMAP.md`](ROADMAP.md).

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
(`claude` default, `codex`).

**The maker is never the grader.** This holds even inside a single lane:
under the Tooling Exception a single implementer writes the tooling in one
pass, but never grades its own verification — get a fresh-context second
read before trusting a result. See `3-lane-protocol.md` § Tooling Exception
and `docs/decisions/ADR-002-*`.

**Changes here propagate.** A rule file edited in this repo reaches every
project through `sync_rules.py`. Treat a rules edit as a change to every
project at once, not a local one.

## Claude Code temp off tmpfs (harmonic-forge#1655)

Claude Code writes its session temp and scratchpad under `/tmp/claude-<uid>/`
unless `CLAUDE_CODE_TMPDIR` says otherwise, and `/tmp` here is an
inode-capped tmpfs. Every Claude lane launch exports
`CLAUDE_CODE_TMPDIR="$HOME/.cache/claude-tmp"` (a value already set is kept),
creates it, and refuses to launch if it cannot. `TMPDIR` is left alone.

A non-lane session does not go through the launcher, so set it yourself: add
this line to your shell profile and create the directory once.

```bash
export CLAUDE_CODE_TMPDIR="$HOME/.cache/claude-tmp"
```

```bash
mkdir -p "$HOME/.cache/claude-tmp"
```

harmonic-forge#1653's reaper ages old session directories out of that root.

## Per-user instance settings (harmonic-forge#1505)

Forge is a public repo, so per-user values never live in it. They are read
from `~/.config/harmonic-forge/instance.toml` (or
`$XDG_CONFIG_HOME/harmonic-forge/instance.toml`), which holds two keys:

```toml
timezone = "America/Los_Angeles"
lane_hook_overlay = "operator"
```

`lane_hook_overlay` (harmonic-forge#1525) chooses the lane hook set every
Claude lane launch builds. `"operator"` merges
`tools/lane/lane_hooks.operator.json` (the belt and memory hooks) into the core
`tools/lane/lane_hooks.json`; `"none"` builds the core set only and prints, on
every launch, that the operator hooks are not wired. Read it only through
`tools/settings/instance.py` `lane_hook_overlay()`. It has no default: a missing
file, a missing key or any other value refuses every Claude lane launch, so a
new adopter declares it before the first launch. The launcher accepts
`XDG_CONFIG_HOME` only as `$HOME/.config` (or unset, empty or relative), so the
declaration cannot be relocated through the environment.

The file is refused inside any git checkout, so it is never committed.
Residual (harmonic-forge#1525): an instance.toml under any git work tree
refuses every launch, including a HOME that is itself a git checkout, while a
bare-repo dotfiles layout used with `--work-tree=$HOME`, and `git add -f`, are
out of reach of any `.git` check; revisit under harmonic-forge#1523.

Read the timezone only through `timezone()`. A missing file or
key returns `America/Los_Angeles`, reported as defaulted; an invalid zone is an
error. Every current timestamp stays UTC. `tools/settings/check_zone_literals.py`
refuses a hardcoded `ZoneInfo("...")` anywhere else in `tools/` or `skills/`.
It is a tripwire, not a proof: a zone held in a variable first, an assignment
alias, `**kwargs` and `from_file` pass it by design (its docstring lists them),
so review still owns those.
