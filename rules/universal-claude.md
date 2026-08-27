# Universal Claude Code CLI Mechanics (ALL PROJECTS)

Claude-Code-CLI-specific mechanics for the Lane 1 (Blueprint) role.
Combine with `universal-lane1.md` (role requirements), `universal-agent.md`
(all agents), the project's own `CLAUDE.md`, and `3-lane-protocol.md`.

## Post-merge worktree cleanup mechanics (harmonic-forge#131)

Run `git worktree list`, and if any worktree (a Lane 2 dedicated
per-issue scratch checkout, or a shared lane2/lane3 worktree) is still
checked out on a branch just merged, confirm it's clean (`git status
--short` empty) and remove or detach it. See `universal-lane1.md`'s
Post-merge worktree cleanup section for the obligation this satisfies.

This closes a real, recurring friction: a stray worktree left parked on
an already-merged branch repeatedly blocks the sibling-branch-overlap
check in `l1_post.py`'s `active_worktree_branches()` for the *next*
issue's readiness check, each time costing a manual diagnosis-and-fix
round-trip (hit 3+ times in one session, hrse#490/#500/#511; recurred
again the same night as this fix, hrse#566/#575/#578).

## Advisory trigger invocation mechanics

Use the runtime-supported advisory mechanism with a bounded payload; do
not assume any removed Devin/Cascade profile, model pin, or transport is
active. See `universal-lane1.md`'s Advisory triggers section for which
role to use when.

## Memory protocol

Claude Code maintains persistent, file-based memory outside project
repos. Read it when relevant; write only durable operator feedback,
meaningful state changes, or recurring patterns. Do not save facts
derivable from code or Git.

## Tool-use safeguards

- Prefer `gh api` REST endpoints over GraphQL-backed `gh` subcommands
  (`gh issue view/comment/close/create`, `gh pr create/merge/checks`)
  wherever a REST equivalent exists — see `universal-agent.md`'s "GitHub
  API — Prefer REST over GraphQL" section for the full mapping and why.
  Projects v2 board operations are the one confirmed GraphQL-only
  exception.
- For multiline/code-block GitHub comments: see `3-lane-protocol.md`'s
  "GitHub Comment Formatting" section (harmonic-forge#170 dedupe) — the
  full rule, including the mandatory fetch-back self-check after posting,
  lives there and applies to every lane/tool, not just Claude Code.
- For Git commands that could open an editor/hook, set
  `GIT_EDITOR=true EDITOR=: NO_COLOR=1` and use `--no-edit --no-verify`
  where applicable.
- On one Git/GitHub failure or apparent hang, stop, report the exact
  error, and wait for direction. Do not blindly retry variants.
- Default to the `Monitor` tool for any wait on an external,
  indeterminate-duration event (a GitHub comment landing, CI finishing, a
  file appearing) rather than a raw `Bash --run_in_background` polling
  loop — `Monitor` surfaces a stalled or errored wait instead of sitting
  silent until timeout. `Bash --run_in_background` stays correct for a
  single bounded wait with a clear exit condition (harmonic-forge#312).
- **Every merge silently closes a stacked child PR pointing at the merged
  branch** — not only `gh pr merge --delete-branch`. Every project repo
  here has `delete_branch_on_merge: true` (confirmed live across
  harmonic-forge/hrse/cymagraph-infra), so the REST merge form
  (`gh api -X PUT .../pulls/N/merge`) and the GitHub web merge button
  delete the branch too, and GitHub does **not** auto-retarget the child
  — it closes (real incident, hrse#812). Before merging any PR, check
  `gh pr list --json number,baseRefName` for a child based on it, and
  retarget first if one exists.
- `git checkout`/`checkout -b` does **not** branch-scope uncommitted
  tracked changes — they follow the working tree across the switch. A
  branch created to isolate one edit can silently carry unrelated
  in-progress changes onto it; check `git status` before assuming a fresh
  branch starts clean.
