#!/usr/bin/env bash
# The closed agent registry (harmonic-forge#322 AC2/AC3/AC9, ADR-007 § 3).
#
# Sourced by tools/lane/_cli_launch.sh, itself sourced by lane1/lane2/lane3 --
# the same one-line pattern _gh_config_dir.sh already established. No parse
# step that can fail at launch, and no dependency added to the one script that
# must work before anything else does (ADR-007 § 9: "the launchers themselves
# become the highest-consequence code in the platform").
#
## Why a table and not a `case`
#
# The handoff argued a `case` cannot satisfy AC3. That is not actually true --
# `*) echo ...; exit 1;;` rejects an unknown agent perfectly well. The real
# argument is AC2 and AC9: the registry has to hold *data* per agent -- a
# version floor, a per-lane flag set, a per-lane deny list, an operator-facing
# display name. A `case` holds control flow, not a table, and expressing four
# parallel attributes across three agents as branching logic is exactly where
# drift between "the flags Lane 3 injects" and "the flags Lane 3 refuses to
# have removed" becomes invisible. Those two lists MUST be derivable from one
# declaration or AC4 rots the first time someone adds a flag to one and not the
# other. Here they are: AGENT_LANE_POLICY declares the flag, and
# registry_lane_denied_tokens derives the deny list from it.
#
## Fields
#
# Per agent:
#   AGENT_DISPLAY            operator-facing name -- AC7's `--why` string
#   AGENT_VERSION_MIN        minimum supported version, floored at the MINOR
#   AGENT_VERSION_QUALIFIED  the exact patch version qualification was run at
#   AGENT_ENV_PREFIX         env(1) words prepended to the launch command;
#                            a token `VAR=@default@` means `VAR=${VAR:-default}`
#   AGENT_DEFAULT_FLAG       a flag injected unless the caller passed it
#   AGENT_DEFAULT_FLAG_ENV   env var supplying that flag's value
#   AGENT_DEFAULT_FLAG_VALUE fallback when that env var is unset
#   AGENT_POLICY_FLAG        the flag a per-lane policy file is passed with
#   AGENT_POLICY_CHECK       precondition check on that file: `toml` or `none`
#
# Per agent x lane, keyed "<agent>:<lane>":
#   AGENT_LANE_POLICY        policy filename under tools/lane/policies/, or ""
#                            for a DECLARED-EMPTY slot (NC7 -- harmonic-forge#326
#                            fills gemini:3 rather than reopening the launcher)
#
## Version floors are MINOR, not patch (AC9)
#
# Deliberate. Two of the three CLIs moved between this issue's handoff
# (2026-08-22: claude 2.1.239, codex-cli 0.149.0) and its implementation
# (2026-08-28: 2.1.250, 0.150.1). ADR-007 already handles version-specific
# qualification separately, in the parity suite (harmonic-forge#325) -- "a cell
# is qualified against a CLI version… a CLI upgrade invalidates the cell until
# the suite is re-run". A patch-pinned floor HERE would just generate false
# alarms on every routine `npm -g` update while duplicating that mechanism. A
# floor's job is catching a genuinely too-old CLI; the qualified patch version
# is recorded alongside so the parity suite's claim stays precise.

declare -A AGENT_DISPLAY=(
  [claude]="Claude Code"
  [codex]="Codex"
  [gemini]="Gemini"
)

declare -A AGENT_VERSION_MIN=(
  [claude]="2.1"
  [codex]="0.150"
  [gemini]="0.56"
)

declare -A AGENT_VERSION_QUALIFIED=(
  [claude]="2.1.250"
  [codex]="0.150.1"
  [gemini]="0.56.0"
)

# Gemini's two unsets are load-bearing, not hygiene -- see _cli_launch.sh's
# header and 3-lane-protocol.md § Per-CLI launch wiring for the full reasoning
# (standing operator directive from harmonic-forge#318; the API-key path fails
# silently and produces identical-looking output). The pager/editor vars fix a
# PTY-only hang found in harmonic-forge#366.
declare -A AGENT_ENV_PREFIX=(
  [claude]=""
  [codex]=""
  [gemini]="env -u GOOGLE_API_KEY -u GEMINI_API_KEY GOOGLE_CLOUD_PROJECT=@hrse-497421@ GIT_PAGER=cat GH_PAGER=cat PAGER=cat GIT_EDITOR=true"
)

# harmonic-forge#179: flag injection broke Codex's own argument parsing, so
# Codex gets bare passthrough. Claude's `--permission-mode` default is
# deliberately suppressed when the caller passes the flag explicitly -- an
# intentional override affordance operators use today, retained unchanged at
# every lane including Lane 3 (harmonic-forge#322, Lane 1 decision 3).
declare -A AGENT_DEFAULT_FLAG=(
  [claude]="--permission-mode"
  [codex]=""
  [gemini]=""
)
declare -A AGENT_DEFAULT_FLAG_ENV=(
  [claude]="LANE_PERMISSION_MODE"
  [codex]=""
  [gemini]=""
)
declare -A AGENT_DEFAULT_FLAG_VALUE=(
  [claude]="auto"
  [codex]=""
  [gemini]=""
)

# Gemini's admin tier is the only mechanism proven live (harmonic-forge#326's
# canary) to survive --yolo and remove denied tools from the model's tool list
# entirely. The CLI does NOT fail closed on a missing or invalid policy file --
# verified live 2026-08-28, both a nonexistent path and broken TOML print only a
# stderr warning and the session starts completely unprotected. So `toml` here
# is a launcher-enforced precondition, not a restatement of CLI behavior.
declare -A AGENT_POLICY_FLAG=(
  [claude]=""
  [codex]=""
  [gemini]="--admin-policy"
)
declare -A AGENT_POLICY_CHECK=(
  [claude]="none"
  [codex]="none"
  [gemini]="toml"
)

# Every agent x lane slot is declared, including the empty ones. An absent key
# is a registry integrity failure (see registry_assert_integrity), never a
# silently-unprotected lane.
declare -A AGENT_LANE_POLICY=(
  [claude:1]="" [claude:2]="" [claude:3]=""
  [codex:1]=""  [codex:2]=""  [codex:3]=""
  [gemini:1]="gemini-lane1.toml"
  [gemini:2]="gemini-lane2.toml"
  # Filled by harmonic-forge#326, exactly as harmonic-forge#322 NC7 designed:
  # this slot was declared empty rather than omitted so #326 could fill a field
  # instead of reopening the launcher. It did -- no change to _cli_launch.sh was
  # needed, and #322's own
  # test_deny_mechanism_is_registry_generic_not_gemini_specific had already
  # proven this exact path injects the policy AND makes the flag un-removable.
  [gemini:3]="gemini-lane3.toml"
)

# The agents this registry knows. `--agent` is CLOSED against this list: an
# unrecognized value is a hard error, never an exec attempt (ADR-007 § 3).
LANE_AGENTS=(claude codex gemini)

# Attributes every registered agent must define. A missing or absent-key
# attribute is a hard failure at launch, not a `:-` default -- see NC5.
_REGISTRY_REQUIRED_ATTRS=(
  AGENT_DISPLAY AGENT_VERSION_MIN AGENT_VERSION_QUALIFIED
  AGENT_ENV_PREFIX AGENT_DEFAULT_FLAG AGENT_DEFAULT_FLAG_ENV
  AGENT_DEFAULT_FLAG_VALUE AGENT_POLICY_FLAG AGENT_POLICY_CHECK
)

_registry_die() {
  echo "lane launcher: $*" >&2
  exit 1
}

# registry_assert_integrity -- NC5.
#
# Verified live: a missing or syntactically-broken registry file lets execution
# continue with a partially- or un-defined registry, no error, even under
# `set -euo pipefail`. Sourcing a nonexistent file is a non-fatal error under
# `set -e`, and a file that dies mid-parse leaves whatever it managed to define.
# Both produce a launcher that reads as protected and is not. So the source is
# guarded at the call site AND the result is asserted here: every registered
# agent has every required attribute DEFINED (the key exists) and, where the
# attribute is not legitimately empty, non-empty.
registry_assert_integrity() {
  local agent attr lane
  [ "${#LANE_AGENTS[@]}" -gt 0 ] \
    || _registry_die "agent registry is empty -- refusing to launch"
  for agent in "${LANE_AGENTS[@]}"; do
    for attr in "${_REGISTRY_REQUIRED_ATTRS[@]}"; do
      local -n _table="$attr"
      [ -v "_table[$agent]" ] \
        || _registry_die "agent registry incomplete: $attr[$agent] is not defined -- refusing to launch"
      unset -n _table
    done
    # These four are never legitimately empty; the flag/policy fields are.
    [ -n "${AGENT_DISPLAY[$agent]}" ] \
      || _registry_die "agent registry: AGENT_DISPLAY[$agent] is empty"
    [ -n "${AGENT_VERSION_MIN[$agent]}" ] \
      || _registry_die "agent registry: AGENT_VERSION_MIN[$agent] is empty"
    [ -n "${AGENT_VERSION_QUALIFIED[$agent]}" ] \
      || _registry_die "agent registry: AGENT_VERSION_QUALIFIED[$agent] is empty"
    [ -n "${AGENT_POLICY_CHECK[$agent]}" ] \
      || _registry_die "agent registry: AGENT_POLICY_CHECK[$agent] is empty"
    for lane in 1 2 3; do
      [ -v "AGENT_LANE_POLICY[$agent:$lane]" ] \
        || _registry_die "agent registry: no policy slot declared for $agent at lane $lane -- declare it empty rather than omitting it (harmonic-forge#322 NC7)"
    done
  done
}

# registry_lookup <table> <key> -- hard-errors on an undefined key.
#
# There is deliberately NO `:-` fallback anywhere in this file (NC5). A default
# turns "the registry does not describe this agent" into "the agent has no
# safety flags," which is the fail-open shape ADR-007 § 7 names.
registry_lookup() {
  local table="$1" key="$2"
  local -n _t="$table"
  [ -v "_t[$key]" ] \
    || _registry_die "agent registry: no $table entry for '$key' -- refusing to launch"
  printf '%s' "${_t[$key]}"
}

# registry_is_agent <name> -- membership test against the closed list.
registry_is_agent() {
  local candidate="$1" agent
  for agent in "${LANE_AGENTS[@]}"; do
    [ "$agent" = "$candidate" ] && return 0
  done
  return 1
}

# registry_agent_for_command <command> -- resolve an agent from a command name.
#
# `LANE_CLI` is retained for aliases such as `claude-api`/`claude-pro` (ADR-007
# § 3), so resolution is by PREFIX against the closed list: `claude-api` is the
# claude agent running a wrapper binary.
#
# LANE_CLI IS CLOSED TOO, and that is a deliberate tightening beyond AC8's
# literal wording -- enumerated in lane3_safety_additions.txt's companion note
# and in this issue's completion report. Before this change, a LANE_CLI that
# matched no branch (`/usr/local/bin/gemini`, say) fell through to bare
# passthrough and silently received NO policy injection and NO version floor. An
# agent-selection path that bypasses the registry is precisely the "reads as
# enforced but isn't" failure this issue exists to remove, so it errors.
# `claude`, `claude-api`, `claude-pro`, `codex` and `gemini` -- every form in
# actual use -- resolve unchanged.
registry_agent_for_command() {
  local command="$1" agent
  for agent in "${LANE_AGENTS[@]}"; do
    case "$command" in
      "$agent"*) printf '%s' "$agent"; return 0 ;;
    esac
  done
  return 1
}

# registry_lane_denied_tokens <agent> <lane>
#
# AC4's deny list, DERIVED from the same declaration that injects the flag --
# never maintained as a second list. If AGENT_LANE_POLICY declares a policy for
# this agent+lane, then that agent's policy flag may not be supplied, removed,
# or contradicted through passthrough args. An empty slot denies nothing,
# because there is nothing to protect.
#
# Deliberately honest about its own reach: this makes the flag un-removable at
# the LAUNCHER, which is the only enforcement point that exists (ADR-007 § 9).
# It does not make the flag a boundary the agent cannot talk past -- see
# lane3_safety_additions.txt for why Codex's `--sandbox read-only` was dropped
# rather than shipped behind a claim this mechanism cannot support.
registry_lane_denied_tokens() {
  local agent="$1" lane="$2"
  local policy flag
  policy="$(registry_lookup AGENT_LANE_POLICY "$agent:$lane")"
  [ -n "$policy" ] || return 0
  flag="$(registry_lookup AGENT_POLICY_FLAG "$agent")"
  [ -n "$flag" ] || return 0
  printf '%s\n' "$flag"
}
