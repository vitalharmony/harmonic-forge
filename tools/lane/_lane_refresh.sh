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
#   LANE_REFRESH_ENV      relinked | ok | provisioned | provision-failed | n/a
#                         (backend/.env; lane3 only, set by the caller, not
#                         this file). `provisioned`/`provision-failed`: the
#                         project declares `lane3_env_task` (harmonic-forge#875)
#                         and that task ran instead of the relink, or it -- or
#                         the lookup of it -- failed and the launch refused.
#
# The env status is part of the refresh.log record (its 8th field). A caller
# that decides it after `lane_refresh` (lane3) sets `_lane_refresh_defer_log=1`
# first, then calls `lane_refresh_log_flush` once the status is known --
# including on every refusal path, so a refused launch is still recorded.
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
# durable record, or, under `_lane_refresh_defer_log`, holds it for
# `lane_refresh_log_flush`. Best-effort: a logging failure (e.g. a read-only
# home in a test fixture) must never fail the launch.
_lane_refresh_log() {
  if [ -n "${_lane_refresh_defer_log:-}" ]; then
    _lane_refresh_pending=("$@")
    return 0
  fi
  _lane_refresh_write "$1" "$2" "$3" "$4" "n/a"
}

# lane_refresh_log_flush -- writes the held record with the caller's
# LANE_REFRESH_ENV as its env field (R-0187: the env status is logged). Writes
# at most once per launch; a no-op when nothing is held.
lane_refresh_log_flush() {
  [ "${#_lane_refresh_pending[@]}" -eq 4 ] || return 0
  _lane_refresh_write "${_lane_refresh_pending[@]}" "${LANE_REFRESH_ENV:-n/a}"
  _lane_refresh_pending=()
}

_lane_refresh_pending=()

_lane_refresh_write() {
  local mode="$1" status="$2" from="$3" to="$4" env_status="$5"
  local logdir="${LANE_REFRESH_LOG_DIR:-$HOME/.local/state/lanes}"
  local logfile="$logdir/refresh.log"
  {
    mkdir -p "$logdir" 2>/dev/null \
      && printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
           "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${_lane_name:-unknown}" \
           "${base:-unknown}" "$mode" "$status" "$from" "$to" \
           "$env_status" >> "$logfile"
  } 2>/dev/null || true
}

## Declared Lane 3 env provisioner (harmonic-forge#875)
#
# R-0188 relinks the gate worktree's backend/.env to the main checkout's on the
# premise that it has no legitimate per-worktree divergence. hrse's
# harmonic-forge#861 made that false: its gate worktree owns a backend/.env
# naming a disposable graph, and every relink put production credentials back.
# A project may therefore declare `lane3_env_task` in projects.toml; both
# `lane3` and `lane3-provision` run it instead of relinking.

# lane3_env_lookup <main_root> -- sets LANE3_ENV_TASK (empty when the project
# declares none) and returns 0, or sets LANE3_ENV_LOOKUP_ERROR and returns 1 on
# ANY lookup failure, python3 itself failing included. Only a clean "declared
# nothing" may fall through to the relink.
#
# WHICH manifest answers is fixed, not configurable (preclose survivor 4): the
# projects.toml of the forge checkout this very file lives in, resolved from
# its own path. FORGE_PROJECTS_MANIFEST is a supported override for every other
# manifest consumer, but here a valid-yet-outdated manifest reached through it
# would answer "declares nothing" and relink production credentials over the
# gate worktree's disposable env -- so it is unset for the accessor, which
# itself reads only its own forge root's manifest too. Tests point the
# launcher at a fixture forge root (a copy of tools/lane + tools/onboard next
# to a fixture projects.toml); there is no test-only override to export.
#
# The value channel is stdout ALONE (preclose survivor 2): stderr goes into
# LANE3_ENV_LOOKUP_ERROR, never into the task name, and a zero-exit answer
# that is not one line matching a mise task-name grammar is a lookup failure,
# not a task to run.
lane3_env_lookup() {
  local main_root="$1" forge_root accessor out="" err="" errf rc=0
  forge_root="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../.." && pwd -P)"
  accessor="$forge_root/tools/onboard/lane3_env_task.py"
  LANE3_ENV_TASK=""
  LANE3_ENV_LOOKUP_ERROR=""
  if ! errf="$(mktemp)"; then
    LANE3_ENV_LOOKUP_ERROR="cannot create a temporary file for the lookup's diagnostics"
    return 1
  fi
  out="$(env -u FORGE_PROJECTS_MANIFEST python3 "$accessor" "$(readlink -f "$main_root")" 2>"$errf")" || rc=$?
  err="$(cat "$errf" 2>/dev/null || true)"
  rm -f "$errf"
  if [ "$rc" -ne 0 ]; then
    LANE3_ENV_LOOKUP_ERROR="${err:-python3 exited $rc with no message}"
    return 1
  fi
  if [ -n "$out" ] && ! [[ "$out" =~ ^[A-Za-z0-9_][A-Za-z0-9_.:-]*$ ]]; then
    LANE3_ENV_LOOKUP_ERROR="the lookup exited 0 but its answer is not a single task name: $(printf '%q' "$out")"
    return 1
  fi
  LANE3_ENV_TASK="$out"
  return 0
}

# lane3_env_run_task <main_root> <target> <task> -- runs the declared task and
# sets LANE3_ENV_OUTCOME to one of:
#   provisioned  the task ran and exited 0               (returns 0)
#   undefined    the main checkout's mise config does not define the task
#   list-failed  mise could not list the main checkout's tasks (e.g. untrusted)
#   task-failed  the task ran and exited non-zero
# returning 1 for every outcome but the first.
#
# The task is resolved and run from the MAIN CHECKOUT, with the gate worktree
# passed as `-- --root <target>` (preclose survivor 1). The declaration comes
# from the always-current forge manifest; resolving the task body in the gate
# worktree instead tied it to whatever ref that worktree holds, so a worktree
# not updated at launch (`--ack-stale`, `skipped-busy`) or a forge merge that
# landed before the project's companion hit "no such task" -- and the printed
# remedy, `lane3-provision`, hit it again. The main checkout is the project's
# current definition, and the task still writes only the gate worktree's own
# disposable-graph files: production credentials never enter that worktree.
# "Not defined" is told apart from "ran and failed" so each refusal names a
# remedy that actually clears it. LANE is unset (as the busy check above does)
# so the project's own lane write guards do not read this as a Lane 3 write.
lane3_env_run_task() {
  local main_root="$1" target="$2" task="$3" names="" rc=0
  LANE3_ENV_OUTCOME=""
  if ! names="$(cd "$main_root" && env -u LANE timeout 30 mise tasks ls --name-only 2>/dev/null)"; then
    LANE3_ENV_OUTCOME="list-failed"
    return 1
  fi
  if ! printf '%s\n' "$names" | grep -qxF -- "$task"; then
    LANE3_ENV_OUTCOME="undefined"
    return 1
  fi
  ( cd "$main_root" && env -u LANE mise run "$task" -- --root "$target" ) >&2 || rc=$?
  if [ "$rc" -ne 0 ]; then
    LANE3_ENV_OUTCOME="task-failed"
    return 1
  fi
  LANE3_ENV_OUTCOME="provisioned"
  return 0
}

# lane3_env_remedy <main_root> <target> <task> <rerun> -- one line naming the
# repair for LANE3_ENV_OUTCOME; <rerun> is the command to run once repaired.
lane3_env_remedy() {
  local main_root="$1" target="$2" task="$3" rerun="$4"
  case "$LANE3_ENV_OUTCOME" in
    undefined)
      echo "the main checkout ($main_root) does not define mise task '$task' -- merge the project's change that adds it and fast-forward the main checkout (git -C $main_root pull --ff-only), then $rerun" ;;
    list-failed)
      echo "mise could not list the main checkout's tasks -- if it refused an untrusted config: (cd $main_root && mise trust), then $rerun" ;;
    *)
      echo "read the task's output above, fix its cause, then $rerun (it re-runs 'mise run $task -- --root $target' from $main_root)" ;;
  esac
}
