#!/usr/bin/env bash
# Shared by lane1/lane2/lane3 (harmonic-forge#305). Scopes gh's active-account
# state to the project being launched into, via GH_CONFIG_DIR -- so a lane
# session in one project never observes (or clobbers) another project's
# `gh auth switch` state. Requires $target to already be set by the caller.
#
# The directory is the account's `gh-as` slot under the slot root, the same one the
# lane entrypoints and git credential helper use -- ONE credential per account
# (harmonic-forge#804). Create a slot once with `gh-as --init <account>`. An
# exported GH_TOKEN/GITHUB_TOKEN is cleared alongside: `gh` ranks a token above
# GH_CONFIG_DIR, so a leftover one would authenticate bare `gh` calls as the wrong account.
#
# The decision is one tested call, manifest_slots.launch_identity (harmonic-forge#1528),
# run as `python3 -I` against the projects.toml of the forge checkout this file lives
# in (FORGE_PROJECTS_MANIFEST unset), so a module planted in the target checkout
# (the cwd) can never shadow the stdlib. It prints a mode, the account, a slot dir
# and a token file, one per line, and this file exports exactly what it is told:
#   lane     an `[accounts.<name>] lane_slots = true` account inside a lane: the
#            lane slot's file token, probed with `gh api user` (harmonic-forge#1396);
#   slot     any other projects.toml account: its slot must exist and authenticate
#            as the account (strict, harmonic-forge#1528);
#   ambient  an account only <target>'s main-checkout .claude/forge.toml declares
#            (harmonic-forge#1527): GH_CONFIG_DIR and tokens are left untouched, and
#            one line says the ambient login is not verified.
# An origin that is missing, unparseable or unregistered, or any lookup failure,
# REFUSES the launch rather than falling through to the global gh login
# (harmonic-forge#1507). Account names and the slot root come from Python only.

# harmonic-forge#1540: a lane never inherits GH_REPO from its launch shell. The gh
# shim's gate (tools/hooks/gh_argv.py) refuses any --repo that disagrees with a set
# GH_REPO, and a Lane 1 session works issues in more than one repo; unset, bare gh
# resolves from the checkout's origin and mise tasks set their own. First, before
# any refusal below, so a refused launch never leaves it set either.
unset GH_REPO

# The slot root must be absolute, so a relative GH_ACCT_HOME cannot resolve through
# the checkout (harmonic-forge#1507); Python's slot_dir refuses it too.
case "${GH_ACCT_HOME:-/}" in
  /*) ;;
  *)
    echo "lane${LANE:-?}: GH_ACCT_HOME '$GH_ACCT_HOME' is not an absolute path -- refusing to launch (harmonic-forge#1507)." >&2
    return 1 2>/dev/null || exit 1
    ;;
esac
_gh_remote="$(git -C "$target" remote get-url origin 2>/dev/null || true)"
_gh_forge="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../.." && pwd -P)"
_gh_rc=0
_gh_out="$(env -u FORGE_PROJECTS_MANIFEST python3 -I -c '
import sys
sys.path.insert(0, sys.argv[1] + "/tools/onboard")
import manifest_slots
sys.exit(manifest_slots.main(["launch", *sys.argv[2:]]))
' "$_gh_forge" "$_gh_remote" "$target" "${LANE:-}" 2>/dev/null)" || _gh_rc=$?
{ read -r _gh_mode; read -r _gh_account; read -r _gh_dir; read -r _gh_tokfile; } <<<"$_gh_out" || true
_gh_done() { unset _gh_remote _gh_forge _gh_rc _gh_out _gh_mode _gh_account _gh_dir _gh_tokfile _gh_tok; unset -f _gh_done; }
if [ "$_gh_rc" -ne 0 ] || [ -z "$_gh_account" ]; then
  echo "lane${LANE:-?}: origin '${_gh_remote:-none}' is not registered in projects.toml or .claude/forge.toml -- refusing to launch on the global gh login (harmonic-forge#1507)." >&2
  _gh_done; return 1 2>/dev/null || exit 1
fi
case "$_gh_mode" in
  lane)
    unset GH_TOKEN GITHUB_TOKEN
    # harmonic-forge#1396: each lane runs on its own least-privilege token, passed
    # as GH_TOKEN from the slot's hosts.yml (the shared keyring entry would
    # otherwise win). No slot, no token or a failed probe refuses the launch.
    _gh_tok="$(awk '/^[[:space:]]*oauth_token:/ {print $2; exit}' "$_gh_tokfile" 2>/dev/null || true)"
    if [ -z "$_gh_tok" ]; then
      echo "lane$LANE: no token in $_gh_tokfile -- refusing to launch on the shared credential (harmonic-forge#1396)." >&2
      echo "Store it with:  GH_CONFIG_DIR=$_gh_dir gh auth login --with-token --insecure-storage" >&2
      _gh_done; return 1 2>/dev/null || exit 1
    fi
    if [ "$(GH_TOKEN="$_gh_tok" GH_CONFIG_DIR="$_gh_dir" gh api user --jq .login 2>/dev/null)" != "$_gh_account" ]; then
      echo "lane$LANE: the token in $_gh_dir does not authenticate as $_gh_account -- refusing to launch (harmonic-forge#1396)." >&2
      _gh_done; return 1 2>/dev/null || exit 1
    fi
    export GH_CONFIG_DIR="$_gh_dir" GH_TOKEN="$_gh_tok"
    ;;
  slot)
    unset GH_TOKEN GITHUB_TOKEN
    if [ ! -d "$_gh_dir" ]; then
      echo "lane${LANE:-?}: ${_gh_remote}'s account '${_gh_account}' (from projects.toml) has no gh slot at $_gh_dir -- refusing to launch (harmonic-forge#1528). Create it with:  gh-as --init ${_gh_account}" >&2
      _gh_done; return 1 2>/dev/null || exit 1
    fi
    _gh_tok="$(GH_CONFIG_DIR="$_gh_dir" gh api user --jq .login 2>/dev/null || true)"
    if [ "${_gh_tok,,}" != "${_gh_account,,}" ]; then
      echo "lane${LANE:-?}: the credential in the slot $_gh_dir does not authenticate as the account projects.toml declares for ${_gh_remote} -- refusing to launch (harmonic-forge#1528)." >&2
      _gh_done; return 1 2>/dev/null || exit 1
    fi
    export GH_CONFIG_DIR="$_gh_dir"
    ;;
  ambient)
    echo "lane${LANE:-?}: ${_gh_remote} is registered only by its .claude/forge.toml; running on the ambient gh login as account '${_gh_account}' is not verified (harmonic-forge#1528)." >&2
    ;;
  *)
    echo "lane${LANE:-?}: ${_gh_remote} resolved to an unknown gh identity mode '${_gh_mode}' -- refusing to launch (harmonic-forge#1528)." >&2
    _gh_done; return 1 2>/dev/null || exit 1
    ;;
esac
_gh_done
