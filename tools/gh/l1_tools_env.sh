#!/usr/bin/env bash
# Lane 1 tooling keeps its own origin/main worktrees (harmonic-forge#762).
#
# Lane 1's posting tools (l1_post.py, post_lane_discussion.py,
# post_lane1_issue.py) read git context from wherever they run and load their
# own code from the harmonic-forge checkout. Either can be stale: on
# 2026-09-21 Lane 1 posted from a main checkout 7 ahead / 4 behind running an
# old l1_post.py. The standing hand procedure -- create a throwaway origin/main
# worktree, provision it, run from it, re-create it when origin/main moves --
# was missed, and left Lane 1's activity in ad-hoc worktrees buried in session
# scratch space that no other lane could see.
#
# Operator ruling 2026-09-26: Lane 1 never opens worktrees by hand; the
# tooling keeps its own. This helper maintains two FIXED, registered
# worktrees and brings each to origin/main on every call:
#   <root>/<project>-l1-tools        the project (git context, Tier 1 runs)
#   <root>/harmonic-forge-l1-tools   the scripts themselves
# Both are ordinary registered worktrees, so `git worktree list` shows them
# to every lane and to the operator.
#
# Usage (sourced):  l1_tools_env <project-root>
# Exports:          L1_TOOLS_PROJECT, L1_TOOLS_FORGE
#
# `--detach` is load-bearing: l1_post.py's active_worktree_branches() reads
# only `branch refs/heads/` porcelain lines, so a tools worktree on a named
# branch would false-positive the overlap check on every post.
#
# Accepted residue, recorded rather than rediscovered: this file itself is
# sourced from ${HARMONIC_FORGE_ROOT:-$HOME/harmonic-forge}, the forge main
# checkout, so it is the one piece of the posting path that can still be
# stale.
#
# Test seams: L1_TOOLS_WORKTREE_ROOT (default ~/Harmonic_Projects/.worktrees),
# L1_TOOLS_PROVISION_CMD (default `mise run worktree-provision`).

l1_tools_env() {
  # One call per shell: this shell already holds shared locks, and a second
  # exclusive request from it would wait on itself.
  if [ -n "${L1_TOOLS_PROJECT:-}" ] && [ -n "${_L1_TOOLS_LOCKED:-}" ]; then return 0; fi
  local project_root="${1:?l1_tools_env: project root required}"
  local forge_root="${HARMONIC_FORGE_ROOT:-$HOME/harmonic-forge}"
  local wt_root="${L1_TOOLS_WORKTREE_ROOT:-$HOME/Harmonic_Projects/.worktrees}"
  local project_top common name
  project_top="$(git -C "$project_root" rev-parse --show-toplevel 2>/dev/null)" \
    || { echo "l1_tools_env: $project_root is not inside a git repository" >&2; return 1; }
  # Named after the REPOSITORY (its main checkout, the parent of the common
  # .git dir), never after whichever worktree the caller ran from -- or a
  # call from hrse2-762-impl would create hrse2-762-impl-l1-tools.
  common="$(git -C "$project_top" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" \
    || { echo "l1_tools_env: cannot resolve the git common dir of $project_top" >&2; return 1; }
  name="$(basename "$(dirname "$common")" | tr '[:upper:]' '[:lower:]')"
  mkdir -p "$wt_root" || return 1

  _l1_tools_ensure "$project_top" "$wt_root/${name}-l1-tools" provision || return 1
  _l1_tools_ensure "$forge_root" "$wt_root/harmonic-forge-l1-tools" "" || return 1
  export L1_TOOLS_PROJECT="$wt_root/${name}-l1-tools"
  export L1_TOOLS_FORGE="$wt_root/harmonic-forge-l1-tools"
  _L1_TOOLS_LOCKED=1
}

# _l1_tools_ensure <source-repo> <worktree-path> <provision|""> -- create,
# repair or refresh one tools worktree, serialized on its own flock. Only ever
# writes inside <worktree-path> (plus the source repo's own ref store via
# fetch and its worktree registry); never touches the source repo's working
# tree, never pushes.
_l1_tools_ensure() {
  local src="$1" path="$2" provision="$3"
  local lock="${path%/*}/.$(basename "$path").lock"
  local fd rc=0 created=""
  exec {fd}>"$lock" || return 1
  flock "$fd" || { exec {fd}>&-; return 1; }

  # All refs, not just main: ready-for-l3 needs a freshly pushed Lane 2
  # branch's commit locally to compute its merge-base with origin/main.
  if ! GIT_TERMINAL_PROMPT=0 timeout 60 git -C "$src" fetch -q --prune origin; then
    echo "l1_tools_env: git fetch failed in $src -- cannot bring $path to origin/main" >&2
    exec {fd}>&-; return 1
  fi

  if [ -e "$path/.git" ] \
     && [ -n "$(git -C "$path" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
    # Nothing legitimate edits a tools worktree: re-create it clean.
    echo "l1_tools_env: $path had tracked changes -- re-creating it clean" >&2
    git -C "$src" worktree remove --force "$path" >/dev/null 2>&1 || rc=$?
  fi
  # A checkout that refuses (e.g. untracked residue in the way of a file
  # origin/main now adds) falls through to re-creation rather than wedging
  # every later call.
  if [ -e "$path/.git" ] && ! git -C "$path" checkout -q --detach origin/main 2>/dev/null; then
    echo "l1_tools_env: could not move $path to origin/main -- re-creating it" >&2
    git -C "$src" worktree remove --force "$path" >/dev/null 2>&1 || rc=$?
  fi
  if [ ! -e "$path/.git" ]; then
    git -C "$src" worktree prune >/dev/null 2>&1 || true
    # A directory here with no .git is not a worktree (an interrupted add or
    # remove): nothing legitimate lives in it, so set it aside, never delete.
    if [ -e "$path" ]; then
      local aside
      aside="$path.stale-$(date +%Y%m%d%H%M%S)"
      mv "$path" "$aside" \
        || { echo "l1_tools_env: $path is not a worktree and could not be moved aside" >&2; exec {fd}>&-; return 1; }
      echo "l1_tools_env: $path was not a worktree -- moved it to $aside" >&2
    fi
    local add_err
    if ! add_err="$(git -C "$src" worktree add -q --detach "$path" origin/main 2>&1)"; then
      echo "l1_tools_env: could not create $path: $add_err" >&2
      exec {fd}>&-; return 1
    fi
    created=1
  fi

  # Provision on every creation: an unprovisioned project tools worktree fails
  # every Tier 1 run at l1_post.py's dependency-directory check. A failed
  # provision removes the worktree it just created, so the next call creates
  # and provisions again instead of reusing an unprovisioned one.
  if [ -n "$created" ] && [ -n "$provision" ]; then
    if ! ( cd "$path" && eval "${L1_TOOLS_PROVISION_CMD:-mise run worktree-provision}" ) >/dev/null 2>&1; then
      echo "l1_tools_env: provisioning $path failed -- removed it; the next call retries" >&2
      git -C "$src" worktree remove --force "$path" >/dev/null 2>&1 || true
      exec {fd}>&-; return 1
    fi
  fi
  # Downgrade to a SHARED lock and keep the fd open for the caller's lifetime
  # (inherited by the tool it runs): a concurrent call's exclusive refresh
  # then waits until this run is done, instead of re-checking-out or removing
  # the worktree under a minutes-long l1_post.py Tier 1 run.
  if [ "$rc" -ne 0 ] || ! flock -s "$fd"; then
    exec {fd}>&-
    return "${rc:-1}"
  fi
  return 0
}
