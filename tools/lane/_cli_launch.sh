#!/usr/bin/env bash
# Shared by lane1/lane2/lane3 (harmonic-forge#318 AC3). Builds the `cli_args`
# array -- the exact command each launcher hands to systemd-inhibit -- from
# $LANE_CLI plus the caller's positional parameters.
#
# Previously each of the three launchers carried an identical inline block
# that appended Claude Code's `--permission-mode` default and nothing else.
# That block is now here, plus the per-CLI environment every non-Claude agent
# needs. Deliberately minimal: this is NOT the closed `--agent` registry
# harmonic-forge#322 designs -- it is the smallest wiring that makes
# `LANE_CLI=gemini lane1` behave correctly, and #322 is expected to replace it.
#
# Requires the caller to have sourced this with its own "$@" in scope.
#
## Gemini -- why the two unsets are mandatory, not hygiene
#
# Operator directive (harmonic-forge#318, 2026-08-21): the Gemini CLI always
# uses the OAuth path; GOOGLE_API_KEY/GEMINI_API_KEY exist for programmatic
# use elsewhere and must never be the CLI's auth path. That requirement is
# NOT satisfied by default and fails SILENTLY -- with both keys present (the
# operator's normal shell state) Gemini CLI prints "Both GOOGLE_API_KEY and
# GEMINI_API_KEY are set. Using GOOGLE_API_KEY" and runs on the API-key
# identity and quota. Nothing errors; the output looks correct. So the unsets
# below are the only thing enforcing the directive.
#
# The unsets are process-scoped (`env -u`), never touching the operator's
# interactive shell -- those keys are wanted there.
#
# But note the real scope: `env` execs the CLI, so the stripped environment
# is inherited by the ENTIRE session subtree, not just the agent process --
# every command that session runs. `GEMINI_API_KEY` in particular is a live
# HRSE2 backend variable read via os.getenv() (gemini_gateway.py,
# routers/system.py), so a backend started from inside a `LANE_CLI=gemini`
# session sees it absent and may report itself unconfigured. That is the
# intended trade (the directive is absolute), but it is a real effect and a
# phantom "unconfigured" finding from inside a Gemini lane session should be
# checked against this before being believed.
#
# GOOGLE_CLOUD_PROJECT is required because the active account is
# Workspace-managed; Google's Code Assist path demands a project id for
# Workspace identities (personal @gmail.com accounts do not need one).
# Without it every invocation dies with ProjectIdRequiredError before any
# tool call. `hrse-497421` is a project id, not a credential -- committing it
# is in scope; no secret appears in this file. Override by exporting
# GOOGLE_CLOUD_PROJECT before launching.
#
# Trap worth knowing: while ~/.gemini/settings.json pins
# security.auth.selectedType = "oauth-personal", a GEMINI_API_KEY in the
# environment is ignored entirely -- its presence is not evidence auth works,
# and its absence is not evidence anything is broken.

_lane_cli="${LANE_CLI:-claude}"
cli_args=()

case "$_lane_cli" in
  claude*)
    cli_args+=("$_lane_cli")
    _has_permission_mode=0
    for _arg in "$@"; do
      if [[ "$_arg" == "--permission-mode" || "$_arg" == --permission-mode=* ]]; then
        _has_permission_mode=1
        break
      fi
    done
    if [ "$_has_permission_mode" -eq 0 ]; then
      cli_args+=(--permission-mode "${LANE_PERMISSION_MODE:-auto}")
    fi
    unset _has_permission_mode _arg
    ;;
  gemini*)
    # Pager/editor vars fix a PTY-only hang (harmonic-forge#366): Gemini's
    # shell tool runs in a PTY, so `git log`/`git show`/`gh api` open a
    # pager or editor that never receives input and never exits headless.
    cli_args+=(env -u GOOGLE_API_KEY -u GEMINI_API_KEY
               "GOOGLE_CLOUD_PROJECT=${GOOGLE_CLOUD_PROJECT:-hrse-497421}"
               GIT_PAGER=cat GH_PAGER=cat PAGER=cat GIT_EDITOR=true
               "$_lane_cli")
    # harmonic-forge#362: the admin-tier policy is the only mechanism
    # proven live (harmonic-forge#326) to survive --yolo and remove denied
    # tools from the model's tool list entirely, so it must be supplied
    # unconditionally here -- never left to the caller's own shell/args.
    # Lane 3 gets no policy from this launcher: harmonic-forge#326 owns its
    # own file and is not yet built, so a Gemini Lane 3 session is not
    # policy-protected today (untouched by this issue's scope).
    _policies_dir="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/policies"
    _policy_path=""
    case "${LANE:-}" in
      1) _policy_path="$_policies_dir/gemini-lane1.toml" ;;
      2) _policy_path="$_policies_dir/gemini-lane2.toml" ;;
    esac
    if [ -n "$_policy_path" ]; then
      # hrse#362 AC5, corrected against live behavior: the CLI itself does
      # NOT fail closed on a missing or invalid --admin-policy file --
      # verified live, 2026-08-28: both a nonexistent path and a
      # syntactically broken TOML file print only a stderr warning
      # (`[ADMIN] Policy file error in ...`) and the session starts anyway,
      # completely unprotected under --yolo (confirmed: write_file
      # succeeded with a missing policy file in the same run that printed
      # the warning). A caller not watching stderr -- exactly this
      # launcher's own non-interactive invocation shape -- would never
      # notice. Fail-closed has to be enforced HERE, at the one place that
      # can still refuse to launch at all, since the CLI itself won't.
      if [ ! -f "$_policy_path" ]; then
        echo "lane launcher: admin policy file missing: $_policy_path -- refusing to launch an unprotected Gemini session (harmonic-forge#362)" >&2
        exit 1
      fi
      if ! python3 -c "import sys,tomllib; tomllib.load(open(sys.argv[1],'rb'))" "$_policy_path" 2>/dev/null; then
        echo "lane launcher: admin policy file is not valid TOML: $_policy_path -- refusing to launch an unprotected Gemini session (harmonic-forge#362)" >&2
        exit 1
      fi
      cli_args+=(--admin-policy "$_policy_path")
    fi
    unset _policies_dir _policy_path
    ;;
  *)
    # codex and anything else: bare passthrough, no injected defaults.
    cli_args+=("$_lane_cli")
    ;;
esac

cli_args+=("$@")
unset _lane_cli
