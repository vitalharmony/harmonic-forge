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
# The account comes from the manifest, never from URL text (harmonic-forge#1507):
# the origin's exact owner/repo is looked up with manifest_identity.account_for
# in the projects.toml of the forge checkout this file lives in
# (FORGE_PROJECTS_MANIFEST is unset for the lookup, as _lane_refresh.sh does).
# An origin projects.toml does not register is looked up in <target>'s
# main-checkout .claude/forge.toml, bound to that checkout's real origin
# (harmonic-forge#1527); the launcher's cwd is never consulted.
# An origin that is missing, unparseable or unregistered, or any lookup
# failure, REFUSES the launch rather than falling through to the global gh
# login; so does a registered repo whose account has no lane slot here.
#
# Reforge (harmonic-forge#1507): the whole origin-to-account decision is one
# tested call, manifest_identity.account_for_origin -- a github.com host (or an
# absolute local fixture path) only, one normalization, one exact refusal rule
# for any key the manifest would rewrite, one direct lookup. It runs as
# `python3 -I`, so a module planted in the target checkout (the cwd) can never
# shadow the stdlib; and the slot root must be an absolute path, so a relative
# GH_ACCT_HOME cannot resolve through the checkout.

# harmonic-forge#1540: a lane never inherits GH_REPO from its launch shell. The gh
# shim's gate (tools/hooks/gh_argv.py) refuses any --repo that disagrees with a set
# GH_REPO, and a Lane 1 session works issues in more than one repo; unset, bare gh
# resolves from the checkout's origin and mise tasks set their own. First, before
# any refusal below, so a refused launch never leaves it set either.
unset GH_REPO

_gh_slots="${GH_ACCT_HOME:-$HOME/.config/gh-accounts}"
case "$_gh_slots" in
  /*) ;;
  *)
    echo "lane${LANE:-?}: GH_ACCT_HOME '$_gh_slots' is not an absolute path -- refusing to launch (harmonic-forge#1507)." >&2
    unset _gh_slots
    return 1 2>/dev/null || exit 1
    ;;
esac
_gh_remote="$(git -C "$target" remote get-url origin 2>/dev/null || true)"
_gh_forge="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../.." && pwd -P)"
_gh_rc=0
_gh_account="$(env -u FORGE_PROJECTS_MANIFEST python3 -I -c '
import pathlib, sys
sys.path.insert(0, sys.argv[1] + "/tools/onboard")
import manifest_identity
print(manifest_identity.account_for_origin(sys.argv[2], checkout=pathlib.Path(sys.argv[3])))
' "$_gh_forge" "$_gh_remote" "$target" 2>/dev/null)" || _gh_rc=$?
if [ "$_gh_rc" -ne 0 ] || [ -z "$_gh_account" ]; then
  echo "lane${LANE:-?}: origin '${_gh_remote:-none}' is not registered in projects.toml or .claude/forge.toml -- refusing to launch on the global gh login (harmonic-forge#1507)." >&2
  unset _gh_remote _gh_slots _gh_forge _gh_rc _gh_account
  return 1 2>/dev/null || exit 1
fi
case "$_gh_account" in
  vitalharmony)
    unset GH_TOKEN GITHUB_TOKEN
    case "${LANE:-}" in
      1|2|3)
        # harmonic-forge#1396: each lane runs on its own least-privilege token,
        # passed as GH_TOKEN from the slot's hosts.yml (the shared keyring entry
        # would otherwise win). No slot, no token or a failed identity probe
        # refuses the launch; a lane never falls back to the shared slot.
        _gh_lane_dir="$_gh_slots/vitalharmony-lane$LANE"
        _gh_lane_tok="$(awk '/^[[:space:]]*oauth_token:/ {print $2; exit}' "$_gh_lane_dir/hosts.yml" 2>/dev/null || true)"
        if [ -z "$_gh_lane_tok" ]; then
          echo "lane$LANE: no token in $_gh_lane_dir/hosts.yml -- refusing to launch on the shared credential (harmonic-forge#1396)." >&2
          echo "Store it with:  GH_CONFIG_DIR=$_gh_lane_dir gh auth login --with-token --insecure-storage" >&2
          return 1 2>/dev/null || exit 1
        fi
        if [ "$(GH_TOKEN="$_gh_lane_tok" GH_CONFIG_DIR="$_gh_lane_dir" gh api user --jq .login 2>/dev/null)" != "vitalharmony" ]; then
          echo "lane$LANE: the token in $_gh_lane_dir does not authenticate as vitalharmony -- refusing to launch (harmonic-forge#1396)." >&2
          return 1 2>/dev/null || exit 1
        fi
        export GH_CONFIG_DIR="$_gh_lane_dir" GH_TOKEN="$_gh_lane_tok"
        unset _gh_lane_dir _gh_lane_tok
        ;;
      *)
        export GH_CONFIG_DIR="$_gh_slots/vitalharmony"
        ;;
    esac
    ;;
  harmonicarchitect)
    export GH_CONFIG_DIR="$_gh_slots/harmonicarchitect"
    unset GH_TOKEN GITHUB_TOKEN
    ;;
  *)
    echo "lane${LANE:-?}: ${_gh_remote} is registered to account '${_gh_account}', which has no lane slot -- refusing to launch (harmonic-forge#1507)." >&2
    unset _gh_remote _gh_slots _gh_forge _gh_rc _gh_account
    return 1 2>/dev/null || exit 1
    ;;
esac
unset _gh_remote _gh_slots _gh_forge _gh_rc _gh_account
