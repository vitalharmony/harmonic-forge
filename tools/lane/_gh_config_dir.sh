#!/usr/bin/env bash
# Shared by lane1/lane2/lane3 (harmonic-forge#305). Scopes gh's active-account
# state to the project being launched into, via GH_CONFIG_DIR -- so a lane
# session in one project never observes (or clobbers) another project's
# `gh auth switch` state. Requires $target to already be set by the caller.
#
# Pre-req: each account needs its own one-time `GH_CONFIG_DIR=<dir> gh auth
# login` (interactive, not scripted here -- see harmonic-forge#305). Both
# directories referenced below already exist and are authenticated as of
# 2026-08-18 (~/.config/gh-vitalharmony, ~/.config/gh-harmonicarchitect --
# the latter already reused by ~/.gitconfig-kenekted's credential helper).
#
# Falls through with no override (today's behavior, a shared global
# ~/.config/gh) when the remote matches neither known account -- never a
# hard failure, since an unrecognized project is not this script's problem.

_gh_remote="$(git -C "$target" remote get-url origin 2>/dev/null || true)"
case "$_gh_remote" in
  *github.com*vitalharmony*)
    export GH_CONFIG_DIR="$HOME/.config/gh-vitalharmony"
    ;;
  *github.com*harmonicarchitect*)
    export GH_CONFIG_DIR="$HOME/.config/gh-harmonicarchitect"
    ;;
esac
unset _gh_remote
