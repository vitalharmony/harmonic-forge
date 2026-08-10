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
