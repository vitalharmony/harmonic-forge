#!/usr/bin/env bash
# Shared checkout-refresh helper for the lane launchers (harmonic-forge#761).
#
# Operator ruling 2026-09-26: no launcher keeps its checkout current, so a
# fresh Lane 2 session can read pre-merge rule text and report a conflict
# main has already fixed (measured: HRSE2-lane2 63 commits behind on
# 2026-09-26, last moved by hand 09-21). The manual repair step
# (`lane3-provision`, run by hand at need) has already failed twice as a
# control. This makes every launcher bring its own checkout to origin/main
# automatically, with the outcome always recorded -- never silent, so a gate
# that repairs its own preconditions still reports honestly on them
# (preserving harmonic-forge#322 AC5's actual goal by a different means: make
# the repair reportable, not forbid it).
#
# Usage: source this file, then call:
#   lane_refresh detached      # lane2, lane3
#   lane_refresh branch        # lane1
#
# Every call sets these variables in the caller's shell (never exported here
# -- the caller, `_cli_launch.sh`, exports what it needs onward):
#   LANE_REFRESH_STATUS   updated | current | skipped-dirty | skipped-diverged
#                         | skipped-busy | fetch-failed | checkout-failed
#                         | ack-stale
#   LANE_REFRESH_DETAIL   a human-readable reason for any status but
#                         `current` (git's own error, the busy report, the
#                         branch that was detached); empty otherwise
#   LANE_REFRESH_FROM     HEAD SHA before this call
#   LANE_REFRESH_TO       HEAD SHA after this call (== FROM unless updated)
#   LANE_REFRESH_ENV      relinked | ok | n/a (backend/.env; lane3 only, set by
#                         the caller, not this file)
#
# NC2 (this codebase's own precedent, harmonic-forge#322): never trust the
# local `origin/main` ref. The update target always comes from a fresh
# `git ls-remote`, never from `git rev-parse origin/main`.
#
# Every git call is individually guarded (`|| rc=$?`) so this function can
# never abort a caller running under `set -euo pipefail` -- a launcher must
# start (or refuse) on its own terms, never die inside this helper from an
# unhandled non-zero exit.

lane_refresh() {
  local mode="$1"
  LANE_REFRESH_STATUS=""
  LANE_REFRESH_DETAIL=""
  LANE_REFRESH_FROM="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
  LANE_REFRESH_TO="$LANE_REFRESH_FROM"

  if [ -n "${lane_ack_stale:-}" ]; then
    LANE_REFRESH_STATUS="ack-stale"
    _lane_refresh_log "$mode" "$LANE_REFRESH_STATUS" "$LANE_REFRESH_FROM" "$LANE_REFRESH_TO"
    return 0
  fi

  # NC4 (same as lane3's existing check): refs/heads/main, not bare `main` --
  # the bare form matches both refs/heads/main and refs/remotes/origin/main on
  # a real repo and returns two lines.
  local remote_out="" remote_rc=0
  remote_out="$(GIT_TERMINAL_PROMPT=0 timeout 30 git ls-remote origin refs/heads/main 2>/dev/null)" \
    || remote_rc=$?
  if [ "$remote_rc" -ne 0 ] || [ -z "$remote_out" ] \
     || [ "$(printf '%s\n' "$remote_out" | wc -l)" -ne 1 ]; then
    LANE_REFRESH_STATUS="fetch-failed"
    _lane_refresh_log "$mode" "$LANE_REFRESH_STATUS" "$LANE_REFRESH_FROM" "$LANE_REFRESH_TO"
    return 0
  fi
  local remote_sha="${remote_out%%$'\t'*}"

  # Bring the object locally (the ls-remote probe above writes no refs; this
  # fetch is the one git call in this helper that mutates the object store).
  local fetch_rc=0
  GIT_TERMINAL_PROMPT=0 timeout 60 git fetch -q origin main >/dev/null 2>&1 || fetch_rc=$?
  if [ "$fetch_rc" -ne 0 ] || ! git cat-file -e "$remote_sha" 2>/dev/null; then
    LANE_REFRESH_STATUS="fetch-failed"
    _lane_refresh_log "$mode" "$LANE_REFRESH_STATUS" "$LANE_REFRESH_FROM" "$LANE_REFRESH_TO"
    return 0
  fi

  local dirty=""
  dirty="$(git status --porcelain --untracked-files=no 2>/dev/null)"

  if [ "$mode" = "detached" ]; then
    if [ -n "$dirty" ]; then
      LANE_REFRESH_STATUS="skipped-dirty"
      _lane_refresh_log "$mode" "$LANE_REFRESH_STATUS" "$LANE_REFRESH_FROM" "$LANE_REFRESH_TO"
      echo "$dirty" | sed 's/^/  /' >&2
      return 0
    fi
    if git merge-base --is-ancestor "$remote_sha" "$LANE_REFRESH_FROM" 2>/dev/null; then
      # HEAD already contains the remote tip -- e.g. a gate run against a
      # Lane 2 branch ahead of main. Record current; do not touch HEAD.
      LANE_REFRESH_STATUS="current"
      _lane_refresh_log "$mode" "$LANE_REFRESH_STATUS" "$LANE_REFRESH_FROM" "$LANE_REFRESH_TO"
      return 0
    fi
    if [ "$LANE_REFRESH_FROM" = "$remote_sha" ]; then
      LANE_REFRESH_STATUS="current"
      _lane_refresh_log "$mode" "$LANE_REFRESH_STATUS" "$LANE_REFRESH_FROM" "$LANE_REFRESH_TO"
      return 0
    fi
    # Preclose finding: a worktree with a branch checked out must not be
    # detached silently. Commits no remote has are never left behind a
    # detach (they would survive on the branch ref, but a session resuming
    # there would be on the wrong commit with no signal) -- refuse instead.
    local branch=""
    branch="$(git symbolic-ref -q --short HEAD 2>/dev/null || true)"
    if [ -n "$branch" ]; then
      local unpushed=0
      unpushed="$(git rev-list --count HEAD --not --remotes 2>/dev/null || echo 0)"
      if [ "$unpushed" -gt 0 ]; then
        LANE_REFRESH_STATUS="skipped-diverged"
        LANE_REFRESH_DETAIL="branch '$branch' has $unpushed commit(s) no remote has"
        _lane_refresh_log "$mode" "$LANE_REFRESH_STATUS" "$LANE_REFRESH_FROM" "$LANE_REFRESH_TO"
        return 0
      fi
    fi
    # Preclose finding: never move a shared worktree out from under a live
    # process (a still-open session, a preview stack). LANE is unset for the
    # check so a previous session of this same lane counts as busy rather
    # than being excluded as "our own session".
    local busy_check busy_out=""
    busy_check="$(dirname "${BASH_SOURCE[0]}")/../worktree/check_worktree_busy.py"
    if [ -f "$busy_check" ] \
       && ! busy_out="$(env -u LANE python3 "$busy_check" . 2>&1)"; then
      LANE_REFRESH_STATUS="skipped-busy"
      LANE_REFRESH_DETAIL="$busy_out"
      _lane_refresh_log "$mode" "$LANE_REFRESH_STATUS" "$LANE_REFRESH_FROM" "$LANE_REFRESH_TO"
      return 0
    fi
    local checkout_err=""
    if checkout_err="$(git checkout -q --detach "$remote_sha" 2>&1)"; then
      LANE_REFRESH_TO="$remote_sha"
      LANE_REFRESH_STATUS="updated"
      [ -n "$branch" ] && LANE_REFRESH_DETAIL="was on branch '$branch' (kept; every commit is on a remote), now detached at origin/main"
    else
      # Preclose finding: not `fetch-failed` -- the fetch succeeded. Carry
      # git's own reason (an untracked file in the way, a stale index.lock)
      # so the refusal names the real cause.
      LANE_REFRESH_STATUS="checkout-failed"
      LANE_REFRESH_DETAIL="$checkout_err"
    fi
    _lane_refresh_log "$mode" "$LANE_REFRESH_STATUS" "$LANE_REFRESH_FROM" "$LANE_REFRESH_TO"
    return 0
  fi

  # mode = branch (lane1). Only ever runs `merge --ff-only`, and only when
  # HEAD is refs/heads/main -- a detached HEAD would let `merge --ff-only`
  # succeed and silently move it, which is the one failure mode this branch
  # exists to avoid.
  local sym_ref=""
  sym_ref="$(git symbolic-ref -q HEAD 2>/dev/null || true)"
  local merge_state=""
  [ -d "$(git rev-parse --git-path rebase-merge 2>/dev/null)" ] 2>/dev/null && merge_state="rebase"
  [ -d "$(git rev-parse --git-path rebase-apply 2>/dev/null)" ] 2>/dev/null && merge_state="rebase"
  [ -f "$(git rev-parse --git-path MERGE_HEAD 2>/dev/null)" ] 2>/dev/null && merge_state="merge"

  if [ "$sym_ref" != "refs/heads/main" ] || [ -n "$merge_state" ]; then
    LANE_REFRESH_STATUS="skipped-diverged"
    _lane_refresh_log "$mode" "$LANE_REFRESH_STATUS" "$LANE_REFRESH_FROM" "$LANE_REFRESH_TO"
    echo "lane1: not on a clean refs/heads/main (detached HEAD, another branch, or a merge/rebase in progress) -- not updating; the checkout may be stale" >&2
    return 0
  fi
  if [ -n "$dirty" ]; then
    LANE_REFRESH_STATUS="skipped-dirty"
    _lane_refresh_log "$mode" "$LANE_REFRESH_STATUS" "$LANE_REFRESH_FROM" "$LANE_REFRESH_TO"
    echo "lane1: tracked changes present -- not updating; the checkout may be stale" >&2
    echo "$dirty" | sed 's/^/  /' >&2
    return 0
  fi

  local merge_out=""
  if merge_out="$(git merge --ff-only "$remote_sha" 2>&1)"; then
    LANE_REFRESH_TO="$(git rev-parse HEAD 2>/dev/null || echo "$remote_sha")"
    if [ "$LANE_REFRESH_TO" = "$LANE_REFRESH_FROM" ]; then
      LANE_REFRESH_STATUS="current"
    else
      LANE_REFRESH_STATUS="updated"
    fi
  else
    # A non-fast-forward `merge --ff-only` means local commits diverge from
    # origin/main -- report it as diverged, same as the detached-HEAD case,
    # never attempt anything destructive.
    LANE_REFRESH_STATUS="skipped-diverged"
    echo "lane1: \`git merge --ff-only\` could not fast-forward -- not updating; the checkout may have local commits origin/main doesn't have" >&2
  fi
  _lane_refresh_log "$mode" "$LANE_REFRESH_STATUS" "$LANE_REFRESH_FROM" "$LANE_REFRESH_TO"
  return 0
}

# _lane_refresh_log <mode> <status> <from> <to> -- appends one line to the
# durable record. Best-effort: a logging failure (e.g. a read-only home in a
# test fixture) must never fail the launch.
_lane_refresh_log() {
  local mode="$1" status="$2" from="$3" to="$4"
  local logdir="${LANE_REFRESH_LOG_DIR:-$HOME/.local/state/lanes}"
  local logfile="$logdir/refresh.log"
  {
    mkdir -p "$logdir" 2>/dev/null \
      && printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
           "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${_lane_name:-unknown}" \
           "${base:-unknown}" "$mode" "$status" "$from" "$to" >> "$logfile"
  } 2>/dev/null || true
}
