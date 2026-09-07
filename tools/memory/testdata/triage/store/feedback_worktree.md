---
name: feedback_worktree
description: Use a dedicated per-issue implementation worktree, never the shared lane worktree.
first_seen: 2026-08-01
instances: 4
metadata:
  type: feedback
---

Create a disposable per-issue worktree before any edit. Editing directly inside
the shared lane worktree, or using `git checkout` to branch-switch inside it,
loses another session's uncommitted work.

**Why:** two lanes share the checkout.
**How to apply:** `git worktree add` off `origin/main`, always.
