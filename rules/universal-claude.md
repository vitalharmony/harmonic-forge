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

- For multiline GitHub comments, use `--body-file` or `jq -Rs '{body: .}'`
  with API `--input -`; never inline multiline `--body` text.
- For Git commands that could open an editor/hook, set
  `GIT_EDITOR=true EDITOR=: NO_COLOR=1` and use `--no-edit --no-verify`
  where applicable.
- On one Git/GitHub failure or apparent hang, stop, report the exact
  error, and wait for direction. Do not blindly retry variants.
