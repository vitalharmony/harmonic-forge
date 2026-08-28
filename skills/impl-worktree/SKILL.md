---
name: impl-worktree
description: Create and manage the disposable per-issue implementation worktree (/tmp/<repo>-<issue>-impl) Lane 2 uses for actual code changes — distinct from the fixed <repo>-lane2/ session worktree. Use when starting implementation work on a specific issue, before making any edit.
---

# impl-worktree

Lane 2's *session* runs in the fixed `<repo>-lane2/` worktree
(3-lane-protocol.md's "Per-Lane Working Directories"). Lane 2's
*implementation work for a given issue* happens in a separate, disposable
worktree created fresh per issue — never directly in `<repo>-lane2/`
itself, and never by branch-switching inside it either.

## Create

```bash
git -C <repo>-lane2 worktree add /tmp/<repo>-<issue>-impl -b feat/<issue>-<short-desc> origin/main
```

- Repo-agnostic path: `/tmp/<repo>-<issue>-impl` (e.g. `/tmp/hrse2-700-impl`,
  `/tmp/harmonic-forge-207-impl`) — never a repo-hardcoded shape. Consistent
  with the sibling `/tmp/<repo>-<issue>-prep` convention (used for rebase/
  conflict-resolution prep work against a shared lane worktree).
- Base the new branch off `origin/main` (fetch first if stale), not off
  whatever the shared `<repo>-lane2/` worktree happens to have checked out.

## Provision

A fresh worktree shares git history with `<repo>-lane2/` but nothing
installed via a package manager — `node_modules`, `.venv`, vendored deps,
and anything else git-ignored never carries over from `git worktree add`.
Install fresh in every subdirectory that carries its own dependency
manifest, before making any edit. An agent hitting a missing binary
(`eslint: command not found`, `ModuleNotFoundError`, etc.) mid-gate is the
sign this step was skipped — report and stop per this project's own
no-ad-hoc-fixes norm; don't self-provision mid-task as a workaround, and
don't guess a repo's package manager or flags. Consult the target repo's
own `CLAUDE.md`/README for its exact install commands.

HRSE2 concretely (two ecosystems, both required):

```bash
(cd /tmp/<repo>-<issue>-impl/frontend && npm ci)
(cd /tmp/<repo>-<issue>-impl/backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt)
```

**This ruling was superseded for HRSE2 by hrse#1356/#1375 — read below
before following the commands above literally.** They remain correct for
any repo without a provisioning tool; HRSE2 now has one, and it symlinks.

HRSE2's `mise run worktree-provision` (`scripts/provision_worktree.py`,
hrse#1356) deliberately **symlinks** `frontend/node_modules` and
`backend/.venv` into the main checkout rather than installing per worktree.
The install cost that buys back is real and recurring, and the operator
ruled (2026-08-28, hrse#1375) to keep the symlinks. What changed to make
that safe is narrower than "the concern was wrong" — each of this ruling's
two original reasons has a different status now, and only one of them is
actually closed:

- **Divergent manifests** — *detected, not prevented.*
  `scripts/check_worktree_provisioned.py` diffs a branch's own
  `package.json`/`package-lock.json`/`requirements*.txt` against its
  baseline and refuses to proceed when a symlinked tree has been outgrown.
  A branch that leaves its manifests alone shares safely; one that doesn't
  is stopped rather than silently served the wrong install.

- **Build-cache state sharing** — *closed by hrse#1375.* The sharper
  version of this problem was not the packages but the build *outputs*
  written inside the shared tree: TypeScript's `tsBuildInfoFile` and
  Vite's `cacheDir` both defaulted under `node_modules`, so every
  provisioned worktree wrote to one physical file (verified live: two
  worktrees' `node_modules/.tmp/tsconfig.app.tsbuildinfo` resolved to the
  same inode). Both now write to a worktree-local `frontend/.buildcache/`
  instead. Worth knowing precisely: on HRSE2 today this was **preventive,
  not corrective** — the project sets `noEmit` with neither `composite`
  nor `incremental`, so `tsc -b` stores no file fingerprints and re-checks
  everything every run, and no false PASS was reproducible. Adding
  `"incremental": true` would have made it live and silent.

- **Concurrent install corruption** — **still open, accepted, unmitigated.**
  An `npm ci` or venv rebuild running in one worktree while another is
  reading that same symlinked tree can still corrupt what the reader sees
  mid-run. Nothing above addresses it: the staleness check runs before
  work starts, not during, and relocating build caches does not touch the
  package tree. It is accepted because the failure needs genuinely
  concurrent installs — rarer than the manifest-drift case that check
  covers — and because the alternative is giving up the install savings
  the tool exists for. **If two sessions may install concurrently, do not
  assume the symlink is safe.**

For a repo with no provisioning tool of its own, the original ruling
stands: install per worktree and accept the seconds.

## Work

Make every edit inside `/tmp/<repo>-<issue>-impl` — never in `<repo>-lane2/`.

**`git checkout`/`git rebase` in a *shared* worktree drags every
uncommitted tracked-file change along regardless of target branch** — this
is exactly the failure mode the disposable impl worktree exists to avoid
by never branch-switching in the shared one at all. `mise run gate-checkout`
exists to make that safe, but it is **Lane 3's tool**, for the fixed
`<repo>-lane3/` worktree specifically (`LANE3_ONLY_TASKS` hard-denies it
for any other `LANE` value) — not something a Lane 2 session reaches for.
If you find yourself wanting `gate-checkout`, that's the signal you're
about to branch-switch in the wrong (shared) worktree; create a fresh
`/tmp/.../impl` worktree instead.

`tools/worktree/check_worktree_busy.py` guards a different, narrower
problem: sequential reuse of the *fixed* `<repo>-lane2/3` worktrees across
different issues with no lock (harmonic-forge#137) — its only caller is
`gate-checkout` itself, passing the shared lane worktree's own path. It
has no role in managing a disposable impl worktree, which nothing else
ever contends for.

## Commit and report

Commit locally in the impl worktree. Post the completion report referencing
this worktree's branch, per the project's own Lane 2 completion convention.
Lane 2 never pushes or merges (3-lane-protocol.md's own governing rule) —
leave the branch there for Lane 1.

## Clean up

Once the issue's work is committed and reported, the worktree should be
removed — not left indefinitely:

```bash
git -C <repo>-lane2 worktree remove /tmp/<repo>-<issue>-impl
git -C <repo>-lane2 worktree prune
```

`/tmp` does not survive reboot, but that alone does not clear
`.git/worktrees/`'s administrative record for a worktree whose directory
is already gone — `worktree prune` is what actually reconciles that.
An impl worktree is disposable by design: never a place to leave unpushed,
uncommitted, or unreported work long-term.
