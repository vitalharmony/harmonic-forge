#!/usr/bin/env bash
# Shared "run the agent, then clean up on exit" wrapper (harmonic-forge#751).
#
# Every launcher used to end with `exec systemd-inhibit ... "${cli_args[@]}"`.
# `exec` replaces the launcher's own shell, so nothing remained to notice the
# terminal window closing (SIGHUP) or the agent process exiting -- and
# HRSE2's Lane 3 preview stack is started as a transient systemd --user
# service specifically so it survives process death, which meant closing the
# window left it running for hours. Real incident, 2026-09-25: all three lane
# windows were closed, and a check afterward found Lane 3's
# `hrse-process-compose-9996.service` (backend :8004, frontend :5175,
# working directory HRSE2-lane3) still active about 4.5 hours later --
# stopped only by hand with `mise run pc-down` in the lane worktree.
#
# This file replaces that final `exec` with `lane_run_with_cleanup`: run the
# launch command as an ordinary FOREGROUND child (never exec, and
# deliberately never backgrounded with `&`), trap HUP/TERM/INT/EXIT to run
# cleanup exactly once, then exit with the child's own status.
#
# Two rejected designs, recorded because both look reasonable and both are
# real, reproduced regressions -- a preclose-inspection pass caught both
# before merge:
#
#  1. `"$@" &` without job control silently redirects the backgrounded
#     command's stdin from /dev/null (documented bash behaviour for async
#     commands run by a non-interactive shell) -- every lane session would
#     launch and then be unable to read a single keystroke.
#  2. Turning on job control (`set -m`) to fix (1) puts the child in its OWN
#     process group, which is no longer the terminal's foreground group. Its
#     first read from the tty then triggers SIGTTIN and the whole session
#     hangs, frozen, with no error printed -- reproduced live under a pty
#     (`STAT T`, child `PGID` != terminal `TPGID`). It also means the
#     terminal's own SIGINT (Ctrl-C) reaches only this wrapper, not the
#     child, so a single Ctrl-C -- the CLI's normal in-session cancel
#     gesture -- would kill the whole session and run cleanup instead of
#     reaching the agent.
#
# The fix for both: don't background the child and don't touch job control
# at all. A plain foreground child (no `&`) stays in the SAME process group
# as this wrapper -- which is already the terminal's foreground group, since
# nothing here ever changed it -- so the child gets full, normal tty access
# (no SIGTTIN) and the kernel delivers HUP/TERM/INT to both processes
# DIRECTLY AND SIMULTANEOUSLY on a real hangup or Ctrl-C (no manual "forward
# the signal to the child" step is needed, or possible to get wrong). The
# only thing this wrapper does on top of a bare `"$@"` is override its OWN
# default (terminating) disposition for HUP/TERM/INT so it survives long
# enough to run cleanup once the foreground command has returned -- bash
# defers a trapped signal's handler until the currently-executing foreground
# command completes, so cleanup runs after the child is actually gone either
# way, exactly as `exec`'s own termination-on-signal did, plus the cleanup
# step `exec` structurally could never reach.
#
# Sourced by lane1/lane2/lane3 after `_cli_launch.sh` has built `cli_args`.
# Caller contract: `$target` (the lane's own worktree, already `cd`-ed into)
# and `$_lane_name` ("lane1"/"lane2"/"lane3") are set by the sourcing
# launcher before `lane_run_with_cleanup` is called.

_lane_cleanup_done=0

# Deliberately project-agnostic in its MECHANISM: it knows nothing about
# HRSE2 or process-compose, only "run `mise run pc-down` in the current
# worktree, if and only if that worktree's own mise config declares a
# `pc-down` task" -- a no-op for any project (including harmonic-forge
# itself) that has no such task.
#
# NOT run for lane1: lane1 always operates in the project's single shared
# main checkout (`tools/lane/lane1`'s own `target="$parent/$base"`), which on
# HRSE2 is the operator's persistent day-to-day dev stack, started by `mise
# run restart`/`pc-up` and expected to keep running across many lane1
# sessions -- it is not a disposable per-lane preview. That checkout
# declares a `pc-down` task too, so the naive "does this worktree have the
# task" check the rest of this function uses would stop the operator's live
# stack the moment an unrelated Lane 1 terminal window closed. Lane 2 and
# Lane 3 each run in their own dedicated, isolated worktree
# (`<project>-lane2`/`-lane3`, distinct ports via `worktree-ports`), which is
# exactly the disposable-preview case this issue exists to clean up after --
# so the check stays generic for those two, and is "normally a no-op" for
# lane2 exactly as the issue anticipates (lane2 does not typically start a
# preview stack itself).
_lane_cleanup() {
  # Idempotent: HUP/TERM/INT each also raise EXIT, and this must run once.
  if [ "$_lane_cleanup_done" -eq 1 ]; then
    return 0
  fi
  _lane_cleanup_done=1

  if [ "${_lane_name:-}" = "lane1" ]; then
    return 0
  fi

  if ! timeout 15 mise tasks ls --name-only 2>/dev/null | grep -qx "pc-down"; then
    return 0
  fi
  if ! timeout 30 mise run pc-down >/dev/null 2>&1; then
    echo "${_lane_name:-lane launcher}: pc-down cleanup failed or timed out -- continuing" >&2
  fi
  return 0
}

# Run "$@" (the systemd-inhibit invocation) as an ordinary foreground child
# instead of `exec`-ing it, so this shell survives to run cleanup once the
# child returns -- whether that is a normal exit or termination by a signal
# both processes received directly (see the file header for why this is
# deliberately NOT `"$@" &`).
#
# All four traps call plain `_lane_cleanup`, with no explicit `exit` of their
# own -- deliberately, and verified live rather than assumed. For a
# NON-interactive bash script blocked on a foreground command, a trapped
# HUP/TERM/INT is deferred until that foreground command actually completes
# (so the child always finishes first, is never cut short), and once the
# trap handler returns, bash terminates the script itself using the
# foreground command's own real exit status -- it does NOT fall through to
# resume the lines after `"$@"` below. Verified for both HUP (child killed by
# the group signal's own default disposition, status propagates as 128+HUP)
# and INT (child that ignores SIGINT via its own `trap '' INT`, matching a
# real CLI's cancel-keystroke handling, runs to its natural completion and
# that command's real exit status is what the wrapper reports -- not a fixed
# code). The EXIT trap fires immediately afterward either way (confirmed
# live), so `_lane_cleanup`'s own idempotency guard, not an explicit exit
# code in each trap, is what keeps this correct.
lane_run_with_cleanup() {
  trap '_lane_cleanup' HUP
  trap '_lane_cleanup' TERM
  trap '_lane_cleanup' INT
  trap '_lane_cleanup' EXIT

  local status=0
  "$@" || status=$?
  _lane_cleanup
  exit "$status"
}
