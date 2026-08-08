# Golden-path service-lifecycle workflow (harmonic-forge#34)

The platform's answer to "how does a project restart its services, run its
verification gate, bump its version, and commit — in one consistent,
enforceable way." Every project designates exactly one such path
(`rules/universal-agent.md` § SERVICE LIFECYCLE & GIT); this is the
reference shape for it, built on **mise** (task runner) + **process-compose**
(service lifecycle) instead of a custom per-project script.

## Why mise + process-compose, not a custom tool

Full decision record: `docs/decisions/ADR-001-adopt-mise-process-compose-over-custom-manager.md`.
Short version: HRSE2 had a working custom script (`hrse_manager.py`,
~1,360 lines across six modules) that did all of this. When decomposing a
plan to build a generalized, public version of it ("paved"), a live OSS
evaluation found ~80% of the scope already existed as mature, actively
maintained tooling — building and maintaining a custom tool to do what
`mise` and `process-compose` already do was reinventing the wheel. Marc's
framing: "I'm not in the tool building business, I want to focus on
CymaGraph, my IP." The custom script was retired entirely once the
replacement proved itself (hrse#224, closed 2026-07-13) — not kept as a
fallback.

## The restart ceremony

`mise run restart` (see `mise.toml` in this directory) does, in order:

1. **Bump the version** (unless `--no-bump`), via a project-supplied
   `scripts/bump_version.py`.
2. **Commit**, via a project-supplied `scripts/git_commit.py` — see "Why
   each piece looks the way it does" below for why the commit step is not
   a simple `git add . && git commit`.
3. **Bring the process-compose stack down and back up** (`pc-down` then
   `pc-up`), so a restart always reflects the code that was just
   committed, not stale running processes.

`mise run check` runs the verification gate (backend typecheck, frontend
build, whatever your project's actual checks are) — **before** any commit
that bumps/pushes, never after. `rules/universal-lane1.md`'s Verification
Standard governs what counts as a real PASS here.

## Why each piece looks the way it does — read before changing the shape

Every non-obvious design choice below exists because of a specific,
documented incident, not a style preference. Changing the shape without
reading why is how the incident recurs.

- **`pc-up`'s `-D` (detached) flag.** `process-compose up` without it runs
  in the foreground and never returns control to the caller, even after
  the stack comes up successfully and every readiness probe passes.
  Without `-D`, a `mise run restart` call from an agent just hangs
  indefinitely — the agent (correctly) doesn't know the stack is actually
  fine, because the command it's waiting on genuinely never exits.
- **`pc-up`'s port-collision preflight loop.** Refuses to start if the
  target ports are already bound by *anything* — the specific failure this
  guards against is a stale process (native, or a previous stack that
  wasn't torn down) silently absorbing the new stack's health checks: the
  readiness probe reports "Ready" because it's polling the *old* process,
  while the actually-managed process never started or crashed on a port
  conflict.
- **`process-compose.yaml`'s `availability.restart: "exit_on_failure"`** on
  the backend service. Without it, a backend that crashes immediately on
  startup can still leave a stale "Ready" status from a prior successful
  probe cycle — the default restart policy doesn't force a fresh
  readiness evaluation on its own.
- **The `commit` task's ordering: stage → compute a transaction-log
  summary from the *staged* diff → append the entry → re-stage → commit,
  all as one commit, never two.** The original custom-script version
  computed and wrote the log entry *after* committing, as a second,
  unstaged file write — which meant the working tree was always left
  dirty by exactly one pending entry, and could never converge to clean
  through the tool's own normal operation. See `tools/transaction-log/`
  in this repo for the reusable implementation and the full incident
  record (hrse#242).
- **`gh-new-issue`'s two-step create-then-add-to-board**, not a single
  call. GitHub's own automation for auto-adding new issues to a project
  board has proven unreliable in practice — the explicit second step is a
  parity requirement carried over from the custom-script era, not
  decoration.
- **`post-comment`'s mandatory self-check (refetch + byte-exact diff).**
  A written rule ("write to a file, post via `--body-file`") recurred as a
  mangled-comment failure **three times** on the same project despite
  being documented twice — see ADR-004 and ADR-005. The lesson generalizes:
  a rule that depends on being remembered at a routine, high-frequency
  moment isn't sufficient on its own; make the compliant path the only
  easy one to invoke instead.
- **`gh-new-issue`/`post-comment` call `tools/gh/` directly, not a
  project-local script copy** (harmonic-forge#53). The original per-project
  scripts hardcoded their repo slug, which caused a real mis-post — this
  tool ran for an `harmonic-forge` issue out of HRSE2 habit and silently
  posted to the wrong repo because of the hardcoded default. `tools/gh/`
  requires `--repo` explicitly (no default) and prints a banner naming the
  resolved target before acting; a project supplies its own slug once via
  `[env]`, not by hand-copying a script.

## What a project has to supply itself

This template shows the *shape*; these two scripts are still
project-specific because they touch project-specific state (your version
file, your repo layout) — `gh_issue.py`/`post_comment.py` are **not**
project-specific anymore, see `tools/gh/README.md` in this repo:

- `scripts/bump_version.py` — reads/writes your project's version file.
- `scripts/git_commit.py` — the three-tier commit-message hierarchy
  (explicit override > auto-bump message > default), plus the
  transaction-log wiring — call into `tools/transaction-log/` from this
  repo rather than reimplementing it.
- `[env]`'s `GH_REPO` (and, if this project has a board,
  `GH_PROJECT_OWNER`/`GH_PROJECT_NUMBER`) — the only project-specific input
  `tools/gh/gh_issue.py`/`post_comment.py` need; see `mise.toml` above.

See HRSE2's actual `mise.toml`/`process-compose.yaml`/`scripts/` for a
concrete, live-verified reference implementation — treat it as a worked
example to adapt, not something to symlink (its container/dashboard/
db-heal tasks are CymaGraph-specific and not part of this golden path).

## The docs/tooling-repo subset (no running artifact)

Not every project has a service to restart or an artifact to version-stamp.
`harmonic-forge`'s own `mise.toml` (repo root) is the sanctioned pattern for
that case (harmonic-forge#54) — resolved via `product-strategy`, not just
deferred: `check` + `commit` + `gh-new-issue` + `post-comment` only, no
`bump`/`restart`/`pc-up`/`pc-down`. A version bump would gate nothing here
— consumers pull this repo via `sync_rules.py --pull`, so the git SHA
already **is** the version, and `harmonic-forge.md`'s own "Draft v0.2" header
is a hand-maintained spec-status marker deliberately bumped at real
milestones, not automated per-commit. Without a bump to hang transaction-
log rotation on, `commit --push` clears the log instead — push is this
kind of repo's genuine "publish" event. Use this subset (not the full
pattern above) for any future docs/tooling-only project; don't invent a
fake `bump`/`restart` ceremony just to match the shape.
