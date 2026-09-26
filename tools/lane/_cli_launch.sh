#!/usr/bin/env bash
# Builds `cli_args` -- the exact command each launcher hands to
# systemd-inhibit -- from the closed agent registry (harmonic-forge#322).
#
# Sourced by lane1/lane2/lane3 after _lane_args.sh has parsed the launcher's own
# options. Replaces the interim `case` over $LANE_CLI that harmonic-forge#318
# landed and ADR-007 § 3 explicitly recorded as a stopgap: an arbitrary string
# execed directly cannot reject an unknown agent, carry a version floor, or hold
# per-lane defaults.
#
# Caller contract:
#   in   LANE                    the lane number, already exported
#   in   lane_agent_requested    `--agent` value, or "" (from _lane_args.sh)
#   in   lane_passthrough        args to forward to the agent CLI
#   in   _lane_name              "lane1"/"lane2"/"lane3", for messages
#   out  cli_args                the launch command
#   out  LANE_AGENT              exported, the resolved agent name
#   out  lane_agent_display      operator-facing agent name, for AC7's --why
#
## On "immutable for the session's lifetime" (AC2)
#
# LANE and LANE_AGENT are exported here and never written again. `readonly` is
# NOT what makes them immutable -- readonly does not survive `exec`, and the
# launcher execs. What is actually true, and what the tests assert, is the
# structural property process environments already have: a child cannot alter
# its parent's environment, so the values every hook subprocess reads are fixed
# by how the session was started. That is the same mechanism 3-lane-protocol.md
# already describes for LANE. This file does not claim to enforce more than
# that, because it cannot.
#
## Gemini's env prefix -- why the two unsets are mandatory, not hygiene
#
# Standing operator directive (harmonic-forge#318): the Gemini CLI always uses
# the OAuth path; GOOGLE_API_KEY/GEMINI_API_KEY exist for programmatic use
# elsewhere and must never be the CLI's auth path. That is NOT the default and
# it fails SILENTLY -- with both keys present (the operator's normal shell
# state) Gemini CLI prints "Both GOOGLE_API_KEY and GEMINI_API_KEY are set.
# Using GOOGLE_API_KEY" and runs on the API-key identity and quota, with no
# error and correct-looking output. The unsets are the only thing enforcing the
# directive.
#
# They are process-scoped (`env -u`), never touching the operator's interactive
# shell. But note the real scope: `env` execs the CLI, so the stripped
# environment is inherited by the ENTIRE session subtree. GEMINI_API_KEY in
# particular is a live HRSE2 backend variable read via os.getenv(), so a backend
# started from inside a Gemini lane session sees it absent and may report itself
# unconfigured. That is the intended trade, but a phantom "unconfigured" finding
# from inside a Gemini lane session should be checked against this before being
# believed.
#
# GOOGLE_CLOUD_PROJECT is required because the active account is
# Workspace-managed; Google's Code Assist path demands a project id for
# Workspace identities. Without it every invocation dies with
# ProjectIdRequiredError before any tool call. `hrse-497421` is a project id,
# not a credential. Override by exporting GOOGLE_CLOUD_PROJECT before launching.

_lane_launch_die() {
  echo "${_lane_name:-lane launcher}: $*" >&2
  exit 1
}

_lane_dir="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"

# NC5: sourcing a missing or half-broken registry is NOT fatal under
# `set -euo pipefail` -- execution continues with a partially-defined registry
# and no error, which is a launcher that reads as protected and is not. Guard
# the source, then assert the result.
# shellcheck source=_agent_registry.sh
source "$_lane_dir/_agent_registry.sh" \
  || _lane_launch_die "agent registry could not be loaded from $_lane_dir/_agent_registry.sh -- refusing to launch"
registry_assert_integrity

## Agent resolution -- `--agent` and LANE_CLI are mutually exclusive (AC3)
#
# ADR-007 § 3 rejects defining a precedence: "the operator who set LANE_CLI in a
# shell profile and then typed --agent gemini gets a session that ignores half
# of what they said and reports nothing. Refusing costs one error message and
# removes a class of confusion entirely."
if [ -n "$lane_agent_requested" ] && [ -n "${LANE_CLI:-}" ]; then
  _lane_launch_die "--agent and LANE_CLI cannot both be set (--agent=$lane_agent_requested, LANE_CLI=$LANE_CLI) -- they are mutually exclusive, not a precedence question (ADR-007 § 3). Unset one."
fi

if [ -n "$lane_agent_requested" ]; then
  # The closed list. An unknown --agent is a hard error and execs NOTHING.
  registry_is_agent "$lane_agent_requested" \
    || _lane_launch_die "unknown agent '$lane_agent_requested' -- the registry knows: ${LANE_AGENTS[*]}. Nothing was launched."
  _lane_agent="$lane_agent_requested"
  _lane_command="$lane_agent_requested"
else
  # LANE_CLI is retained for aliases such as claude-api/claude-pro, resolved by
  # prefix against the same closed list. A LANE_CLI matching no registered agent
  # is ALSO an error -- see registry_agent_for_command's own comment for why
  # this deliberate tightening beyond AC8's literal wording is the right call,
  # and lane3_safety_additions.txt's companion note for its enumeration.
  _lane_command="${LANE_CLI:-claude}"
  if ! _lane_agent="$(registry_agent_for_command "$_lane_command")"; then
    _lane_launch_die "LANE_CLI='$_lane_command' matches no registered agent (${LANE_AGENTS[*]}). An agent-selection path that bypasses the registry receives no per-lane policy and no version floor, so it is refused rather than silently launched unprotected. Use --agent, or a command name prefixed with a registered agent (e.g. claude-api)."
  fi
fi

# AC2. Exported alongside LANE, and never written again after this point.
export LANE_AGENT="$_lane_agent"
lane_agent_display="$(registry_lookup AGENT_DISPLAY "$_lane_agent")"

## AC9 -- minimum-version check
#
# Floored at the minor version deliberately; see _agent_registry.sh's header.
# A CLI that is absent, cannot report a version, or reports one below the floor
# is a hard error: today the first two produce an opaque failure from inside
# systemd-inhibit instead.
_lane_check_version() {
  local command="$1" agent="$2" floor raw detected
  floor="$(registry_lookup AGENT_VERSION_MIN "$agent")"
  command -v "$command" >/dev/null 2>&1 \
    || _lane_launch_die "'$command' is not on PATH -- cannot launch the $agent agent."
  if ! raw="$(timeout 30 "$command" --version 2>/dev/null)"; then
    _lane_launch_die "'$command --version' failed -- cannot verify the $agent minimum version ($floor). Refusing to launch rather than assuming."
  fi
  # `|| true` is load-bearing: without it, a --version output containing no
  # version at all makes the command substitution fail, and `set -e` kills the
  # launcher SILENTLY -- before reaching the explicit error below. Fail loudly
  # or the check is worse than none.
  detected="$(printf '%s' "$raw" | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -1 || true)"
  [ -n "$detected" ] \
    || _lane_launch_die "could not parse a version out of '$command --version' (got: ${raw//$'\n'/ }) -- cannot verify the $agent minimum ($floor)."
  # Numeric major.minor comparison. Patch is recorded, never compared.
  local d_major d_minor f_major f_minor
  IFS=. read -r d_major d_minor _ <<<"$detected"
  IFS=. read -r f_major f_minor _ <<<"$floor"
  if [ "$d_major" -lt "$f_major" ] \
     || { [ "$d_major" -eq "$f_major" ] && [ "$d_minor" -lt "$f_minor" ]; }; then
    _lane_launch_die "$agent $detected is below the supported minimum $floor (qualified at $(registry_lookup AGENT_VERSION_QUALIFIED "$agent")). Upgrade the CLI, or amend the floor in tools/lane/_agent_registry.sh in its own issue."
  fi
}
_lane_check_version "$_lane_command" "$_lane_agent"

## AC4 -- passthrough may not remove or contradict a lane's safety flags
#
# The deny list is DERIVED from the same registry declaration that injects the
# flag (registry_lane_denied_tokens), so the two cannot drift. Rejected, not
# warned about: "an unsafe override must fail the launch, not print a warning
# and proceed" (harmonic-forge#322's dependency-lock comment).
#
# Scope, stated honestly: this makes the flag un-removable AT THE LAUNCHER,
# which ADR-007 § 9 establishes as the only enforcement point that exists. It
# does not make the flag a boundary the agent cannot reason past -- which is
# exactly why Codex's `--sandbox read-only` was dropped from this issue rather
# than shipped behind a claim the mechanism cannot support. See
# lane3_safety_additions.txt.
_lane_denied=()
while IFS= read -r _token; do
  [ -n "$_token" ] && _lane_denied+=("$_token")
done < <(registry_lane_denied_tokens "$_lane_agent" "$LANE")

# _lane_arg_denied <arg> <token> -- does one passthrough word hit one token?
#
# A plain token matches the word exactly or as `<token>=value`. A token that
# ends in `*` (harmonic-forge#756 NC4) is a PREFIX match on a Codex `-c` config
# KEY: it is tested against the word itself (the value word of `-c key=v` or
# `--config key=v`) and against the key inside the glued `-ckey=v` and
# `--config=key=v` forms. The comparison is a quoted-prefix `case`, so the
# `*` is never a glob and a token never matches a path on disk.
_lane_arg_denied() {
  local arg="$1" token="$2" key
  case "$token" in
    *\*)
      key="$arg"
      case "$key" in
        --config=*) key="${key#--config=}" ;;
        -c?*) key="${key#-c}" ;;
      esac
      case "$key" in
        "${token%\*}"*) return 0 ;;
      esac
      return 1
      ;;
  esac
  [ "$arg" = "$token" ] || [ "${arg%%=*}" = "$token" ] && return 0
  # The glued config forms, so `sandbox_workspace_write` (a whole-table
  # override) is refused however the `-c` is spelled.
  case "$arg" in
    --config=*) key="${arg#--config=}" ;;
    -c?*) key="${arg#-c}" ;;
    *) return 1 ;;
  esac
  [ "${key%%=*}" = "$token" ]
}

if [ "${#_lane_denied[@]}" -gt 0 ]; then
  for _arg in "${lane_passthrough[@]}"; do
    for _token in "${_lane_denied[@]}"; do
      if _lane_arg_denied "$_arg" "$_token"; then
        _lane_launch_die "'$_token' is supplied or excluded by the launcher for $_lane_agent at lane $LANE and cannot be set, removed, or contradicted through passthrough arguments (harmonic-forge#322 AC4, ADR-007 § 9). Nothing was launched."
      fi
    done
  done
fi

## harmonic-forge#665 -- launch-default preconditions
#
# Environment that would contradict the launcher's model/effort choice refuses
# the launch; see AGENT_LAUNCH_REFUSED_ENV in the registry for why each one.
#
# The lookup is assigned before the loop on purpose: `set -e` ignores a failing
# command substitution inside a `for` word list, which would turn a missing
# registry entry into a loop that runs zero times and a launch that proceeds.
_lane_refused_env="$(registry_lookup AGENT_LAUNCH_REFUSED_ENV "$_lane_agent")"
for _var in $_lane_refused_env; do
  if [ -n "${!_var:-}" ]; then
    _lane_launch_die "$_var is set -- it would override the lane's launch model/effort ($_lane_refused_env are refused, harmonic-forge#665). Unset it and use LANE_DEFAULT_MODEL / LANE_DEFAULT_EFFORT. Nothing was launched."
  fi
done
# Claude Code also applies a settings file's `env` block to its own process, so
# the same variables arriving that way reach the session and its hooks just the
# same. The launcher has already cd'd into the lane's worktree, so project
# settings resolve against the directory the session will start in.
if [ -n "$_lane_refused_env" ]; then
  _lane_settings_hit="$(python3 - "$_lane_refused_env" \
      "$HOME/.claude/settings.json" "$PWD/.claude/settings.json" \
      "$PWD/.claude/settings.local.json" /etc/claude-code/managed-settings.json <<'PY'
import json, sys
refused = sys.argv[1].split()
for path in sys.argv[2:]:
    try:
        env = json.load(open(path)).get("env") or {}
    except (OSError, ValueError, AttributeError):
        continue
    if not isinstance(env, dict):
        continue
    for name in refused:
        if str(env.get(name) or "").strip():
            print(f"{name} in {path}")
            raise SystemExit(0)
PY
)" || _lane_launch_die "could not read Claude settings files to check for $_lane_refused_env -- refusing to launch rather than assuming none are set."
  [ -z "$_lane_settings_hit" ] \
    || _lane_launch_die "$_lane_settings_hit (settings \`env\` block) would override the lane's launch model/effort ($_lane_refused_env are refused, harmonic-forge#665). Remove it. Nothing was launched."
  unset _lane_settings_hit
fi

# An explicit but EMPTY --model/--effort (`--model=`, or `--model ""`) is
# treated as not given, by operator ruling 2026-09-15: it is dropped from the
# passthrough, so the launch default applies (sonnet, and no --effort). Left in,
# it would suppress the default and hand the CLI nothing.
for _table in AGENT_MODEL_FLAG AGENT_EFFORT_FLAG; do
  _flag="$(registry_lookup "$_table" "$_lane_agent")"
  [ -n "$_flag" ] || continue
  _kept=()
  _n=${#lane_passthrough[@]}
  _i=0
  while [ "$_i" -lt "$_n" ]; do
    _arg="${lane_passthrough[$_i]}"
    if [ "$_arg" = "$_flag=" ]; then
      _i=$((_i + 1))
      continue
    fi
    if [ "$_arg" = "$_flag" ] && [ $((_i + 1)) -lt "$_n" ] && [ -z "${lane_passthrough[$((_i + 1))]}" ]; then
      _i=$((_i + 2))
      continue
    fi
    _kept+=("$_arg")
    _i=$((_i + 1))
  done
  lane_passthrough=("${_kept[@]}")
done
unset _table _flag _i _n _kept

_lane_effort_env="$(registry_lookup AGENT_EFFORT_FLAG_ENV "$_lane_agent")"
if [ -n "$_lane_effort_env" ] && [ -n "${!_lane_effort_env:-}" ]; then
  _lane_effort_levels="$(registry_lookup AGENT_EFFORT_LEVELS "$_lane_agent")"
  _lane_effort_ok=0
  for _level in $_lane_effort_levels; do
    [ "${!_lane_effort_env}" = "$_level" ] && _lane_effort_ok=1
  done
  [ "$_lane_effort_ok" -eq 1 ] \
    || _lane_launch_die "$_lane_effort_env='${!_lane_effort_env}' is not an effort level ($_lane_effort_levels). Nothing was launched."
  unset _lane_effort_levels _lane_effort_ok _level
fi
unset _var _lane_effort_env _lane_refused_env

## Build the launch command
cli_args=()

# 1. The agent's env(1) prefix, if any. A `VAR=@default@` token means
#    `VAR=${VAR:-default}` -- the one substitution this field supports, so the
#    registry can stay declarative without an eval.
_lane_env_prefix="$(registry_lookup AGENT_ENV_PREFIX "$_lane_agent")"
if [ -n "$_lane_env_prefix" ]; then
  for _word in $_lane_env_prefix; do
    case "$_word" in
      *=@*@)
        _var="${_word%%=*}"
        _default="${_word#*=@}"
        _default="${_default%@}"
        cli_args+=("$_var=${!_var:-$_default}")
        ;;
      *)
        cli_args+=("$_word")
        ;;
    esac
  done
  unset _word _var _default
fi

# 2. The command itself -- the literal LANE_CLI value when an alias was used,
#    so claude-api/claude-pro still exec the wrapper rather than plain claude.
cli_args+=("$_lane_command")

# 3. The agent's launch-default flags, each unless the caller passed it
#    explicitly (spaced or `=` form, before or after a bare `--`). This is the
#    harmonic-forge#179 override affordance, retained unchanged at every lane
#    including Lane 3 (harmonic-forge#322, Lane 1 decision 3). `--model` and
#    `--effort` joined `--permission-mode` in harmonic-forge#665; an empty
#    resolved value injects nothing, which is how an unset effort stays unset.
_lane_inject_default() {
  local flag_table="$1" env_table="$2" value_table="$3"
  local flag env_name value arg
  flag="$(registry_lookup "$flag_table" "$_lane_agent")"
  [ -n "$flag" ] || return 0
  for arg in "${lane_passthrough[@]}"; do
    if [ "$arg" = "$flag" ] || [ "${arg%%=*}" = "$flag" ]; then
      return 0
    fi
  done
  env_name="$(registry_lookup "$env_table" "$_lane_agent")"
  value="$(registry_lookup "$value_table" "$_lane_agent")"
  if [ -n "$env_name" ] && [ -n "${!env_name:-}" ]; then
    value="${!env_name}"
  fi
  [ -n "$value" ] || return 0
  cli_args+=("$flag" "$value")
}
_lane_inject_default AGENT_DEFAULT_FLAG AGENT_DEFAULT_FLAG_ENV AGENT_DEFAULT_FLAG_VALUE
_lane_inject_default AGENT_MODEL_FLAG AGENT_MODEL_FLAG_ENV AGENT_MODEL_FLAG_VALUE
_lane_inject_default AGENT_EFFORT_FLAG AGENT_EFFORT_FLAG_ENV AGENT_EFFORT_FLAG_VALUE
unset -f _lane_inject_default

# 3b. A brevity directive for the chat surface (hrse#1703 AC6). Comment-time
#     validation (`l1_post.py`'s lead-block/cap requirement) cannot see what a
#     lane types before it ever reaches a `--file`; this is the injection
#     point AGENT_DEFAULT_FLAG above already proves reaches every session of
#     an agent that takes one. Unconditional -- unlike the default flag, this
#     is directive content, not a mode a caller would ever want to override
#     per-invocation, so there is no passthrough-already-given check to make.
_lane_system_prompt_flag="$(registry_lookup AGENT_SYSTEM_PROMPT_FLAG "$_lane_agent")"
if [ -n "$_lane_system_prompt_flag" ]; then
  cli_args+=("$_lane_system_prompt_flag" \
    "Lead every posted lane artifact (handoff, ready-for-l3, rework, spec, gate-result, sweep, AE) with its required one-line summary fields before any evidence, per hrse#1703 -- l1_post.py/post_lane_discussion.py refuse a post that buries them or that lets the lead itself run past 1,200 bytes. Compose it correctly the first time.")
fi
unset _lane_system_prompt_flag

# 4. The lane's policy file, if the registry declares one for this agent+lane.
#
#    hrse#362 AC5, corrected against live behavior: the Gemini CLI does NOT fail
#    closed on a missing or invalid --admin-policy file -- verified live
#    2026-08-28, both a nonexistent path and syntactically broken TOML print
#    only a stderr warning (`[ADMIN] Policy file error in ...`) and the session
#    starts anyway, completely unprotected under --yolo. A caller not watching
#    stderr -- exactly this launcher's own non-interactive shape -- would never
#    notice. So fail-closed is enforced HERE, at the one place that can still
#    refuse to launch at all. Carried forward from harmonic-forge#362 as a
#    registry-declared precondition rather than a Gemini special case (NC8).
_lane_policy_file="$(registry_lookup AGENT_LANE_POLICY "$_lane_agent:$LANE")"
if [ -n "$_lane_policy_file" ]; then
  _lane_policy_flag="$(registry_lookup AGENT_POLICY_FLAG "$_lane_agent")"
  [ -n "$_lane_policy_flag" ] \
    || _lane_launch_die "agent registry: $_lane_agent declares a lane-$LANE policy but no AGENT_POLICY_FLAG -- refusing to launch a policy that cannot be passed."
  _lane_policy_path="$_lane_dir/policies/$_lane_policy_file"
  case "$(registry_lookup AGENT_POLICY_CHECK "$_lane_agent")" in
    toml)
      [ -f "$_lane_policy_path" ] \
        || _lane_launch_die "policy file missing: $_lane_policy_path -- refusing to launch an unprotected $lane_agent_display session (harmonic-forge#362)"
      # python3/tomllib is a load-bearing runtime dependency of the launcher
      # path itself, not an optional nicety -- named explicitly because this is
      # the one script that must work before anything else does.
      python3 -c "import sys,tomllib; tomllib.load(open(sys.argv[1],'rb'))" "$_lane_policy_path" 2>/dev/null \
        || _lane_launch_die "policy file is not valid TOML: $_lane_policy_path -- refusing to launch an unprotected $lane_agent_display session (harmonic-forge#362)"
      ;;
    none)
      : # declared: this agent's policy needs no precondition check
      ;;
    *)
      _lane_launch_die "agent registry: unknown AGENT_POLICY_CHECK '$(registry_lookup AGENT_POLICY_CHECK "$_lane_agent")' for $_lane_agent"
      ;;
  esac
  cli_args+=("$_lane_policy_flag" "$_lane_policy_path")
  unset _lane_policy_flag _lane_policy_path
fi

# 4b. The lane's sandbox/add-dir grant, if the registry declares one for this
#     agent+lane (harmonic-forge#644). Injected before passthrough so a
#     `resume`/`exec` subcommand supplied as the caller's own argument still
#     receives it (`codex --sandbox workspace-write --add-dir <testplan>
#     resume --last`) -- the same reasoning AGENT_DEFAULT_FLAG already relies
#     on in step 3. `--add-dir` is injected unconditionally (Codex accepts
#     repeated `--add-dir` with no conflict, verified live) so a caller's own
#     `--add-dir` still applies alongside it; `--sandbox` is never
#     conditionally injected here at all -- a caller-supplied one is refused
#     outright by the AC4 deny check above, so this always fires.
_lane_sandbox="$(registry_lookup AGENT_LANE_SANDBOX "$_lane_agent:$LANE")"
if [ -n "$_lane_sandbox" ]; then
  cli_args+=("--sandbox" "$_lane_sandbox")
fi
unset _lane_sandbox
#
#     harmonic-forge#756: the slot is a space-separated LIST relative to $HOME.
#     Each directory is created first (Codex refuses an `--add-dir` that does
#     not exist), then granted. For Codex, TMPDIR is pointed at the lane's
#     private temp root -- one of those add-dirs -- because
#     AGENT_SESSION_FLAGS removes `/tmp` and `$TMPDIR` as writable roots; a
#     TMPDIR left at `/tmp` would make every `mktemp` in the session fail.
#     Claude and Gemini keep the caller's TMPDIR untouched.
_lane_add_dir="$(registry_lookup AGENT_LANE_ADD_DIR "$_lane_agent:$LANE")"
_lane_add_dirs=()
read -ra _lane_add_dirs <<<"$_lane_add_dir"
for _d in "${_lane_add_dirs[@]}"; do
  mkdir -p "$HOME/$_d" \
    || _lane_launch_die "could not create the lane add-dir $HOME/$_d -- refusing to launch (harmonic-forge#756)"
  cli_args+=("--add-dir" "$HOME/$_d")
done
if [ "$_lane_agent" = codex ]; then
  export TMPDIR="$HOME/.cache/codex-lane-tmp/lane$LANE"
  mkdir -p "$TMPDIR" \
    || _lane_launch_die "could not create the Codex lane TMPDIR $TMPDIR -- refusing to launch (harmonic-forge#756)"
fi
unset _lane_add_dir _lane_add_dirs _d

# 4c. The agent's session flags, at every lane (harmonic-forge#754). For Codex
#     this is `--no-daemon`: without it the TUI runs every tool command, hook
#     and MCP server inside a shared app-server daemon that never saw LANE, so
#     every LANE-keyed guard is silently off. Unconditional, before passthrough
#     so an `exec`/`resume` subcommand still receives it, and un-removable: the
#     AC4 deny check above already refused any AGENT_SESSION_DENIED token.
_lane_session_flags="$(registry_lookup AGENT_SESSION_FLAGS "$_lane_agent")"
for _word in $_lane_session_flags; do
  cli_args+=("$_word")
done
unset _lane_session_flags _word

# 5. The caller's own arguments, last.
cli_args+=("${lane_passthrough[@]}")

unset _lane_agent _lane_command _lane_dir _lane_env_prefix \
      _lane_policy_file _lane_denied _token _arg

## Platform-rules sync -- immediately before the final exec (harmonic-forge#651)
#
# A fast-forward-only pull of ~/harmonic-forge itself. The 2026-09-14 incident
# was a Lane 1 session running belt-mode tooling from a checkout 3 commits
# behind origin/main -- both the stale SKILL.md prose AND the stale script
# that still accepted a retired flag were served from the same stale
# checkout, so the launcher is the one place that can close that gap for
# every subsequent hook/skill/tool invocation in the session. Best-effort by
# design: this must never block session startup -- a warning and continue on
# any failure (missing sync_rules.py, no network, merge conflict a
# fast-forward-only pull refuses, timeout).
if ! timeout 30 python3 "$HOME/harmonic-forge/sync_rules.py" --pull >&2; then
  echo "${_lane_name:-lane launcher}: sync_rules.py --pull failed or timed out -- continuing with the current ~/harmonic-forge checkout" >&2
fi
