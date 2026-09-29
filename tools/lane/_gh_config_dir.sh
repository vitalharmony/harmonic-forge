#!/usr/bin/env bash
# Shared by lane1/lane2/lane3 (harmonic-forge#305). Scopes gh's active-account
# state to the project being launched into, via GH_CONFIG_DIR -- so a lane
# session in one project never observes (or clobbers) another project's
# `gh auth switch` state. Requires $target to already be set by the caller.
#
# The directory is the account's `gh-as` slot (${GH_ACCT_HOME:-~/.config/gh-accounts}
# /<account>), the same one the lane entrypoints and git credential helper use --
# ONE credential per account (harmonic-forge#804). Create a slot once with
# `gh-as --init <account>`. The older ~/.config/gh-vitalharmony and
# ~/.config/gh-harmonicarchitect directories are retired and no longer read. An exported
# GH_TOKEN/GITHUB_TOKEN is cleared alongside: `gh` ranks a token above GH_CONFIG_DIR, so a
# leftover one would authenticate bare `gh` calls as the wrong account.
#
# Falls through with no override (today's behavior, a shared global
# ~/.config/gh) when the remote matches neither known account -- never a
# hard failure, since an unrecognized project is not this script's problem.

_gh_slots="${GH_ACCT_HOME:-$HOME/.config/gh-accounts}"
_gh_remote="$(git -C "$target" remote get-url origin 2>/dev/null || true)"
case "$_gh_remote" in
  *github.com*vitalharmony*)
    export GH_CONFIG_DIR="$_gh_slots/vitalharmony"
    unset GH_TOKEN GITHUB_TOKEN
    ;;
  *github.com*harmonicarchitect*)
    export GH_CONFIG_DIR="$_gh_slots/harmonicarchitect"
    unset GH_TOKEN GITHUB_TOKEN
    ;;
esac
unset _gh_remote _gh_slots
