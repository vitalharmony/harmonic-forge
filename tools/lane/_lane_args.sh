#!/usr/bin/env bash
# Shared lane-launcher argument parsing (harmonic-forge#322 AC1/AC3).
#
# Sourced by lane1/lane2/lane3 with their own "$@" in scope, BEFORE anything
# else -- lane3 in particular needs `--ack-stale` resolved before its drift
# checks run, which is well before the launch command is built.
#
# Caller contract:
#   in   $@                      the launcher's own arguments
#   in   _lane_allow_ack_stale   1 on lane3, unset/0 elsewhere
#   out  lane_agent_requested    the `--agent` value, or "" if not given
#   out  lane_ack_stale          the `--ack-stale` reason, or "" if not given
#   out  lane_passthrough        array of args forwarded to the agent CLI
#
## Parsing rule, and why it is this one
#
# Lane options (`--agent`, and `--ack-stale` on lane3) are recognized anywhere
# before a bare `--`; everything else is passthrough, in order. After a bare
# `--`, every remaining argument is forwarded verbatim -- including a literal
# `--agent`, which is the escape hatch if an agent CLI ever grows a flag by
# that name.
#
# The alternative -- "the first non-lane-option ends parsing" -- was rejected
# because it breaks the shapes actually in use: `lane1 -p "some prompt"` would
# stop parsing at `-p`, which is fine, but `lane2 --agent codex -p x` and
# `lane2 -p x --agent codex` would then behave differently for no reason an
# operator can see. Scanning the whole pre-`--` argument list makes position
# irrelevant, which is what an operator assumes.
#
# The bare `--` itself is consumed, not forwarded.
#
# AC8 note: none of the four argument shapes harmonic-forge#363 exercised
# contains `--agent`, `--ack-stale`, or `--`, so every pre-existing invocation
# parses to exactly its own arguments as passthrough and the launch tuple is
# unchanged. That is asserted, not assumed -- see baseline_capture.py.

lane_agent_requested=""
lane_ack_stale=""
lane_passthrough=()

_lane_args_die() {
  echo "${_lane_name:-lane}: $*" >&2
  exit 1
}

_lane_args_need_value() {
  # $1 = option name, $2 = how many args remain after the option
  [ "$2" -gt 0 ] || _lane_args_die "$1 requires a value"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --)
      shift
      lane_passthrough+=("$@")
      break
      ;;
    --agent)
      _lane_args_need_value --agent "$(($# - 1))"
      lane_agent_requested="$2"
      [ -n "$lane_agent_requested" ] || _lane_args_die "--agent requires a value"
      shift 2
      ;;
    --agent=*)
      lane_agent_requested="${1#--agent=}"
      [ -n "$lane_agent_requested" ] || _lane_args_die "--agent requires a value"
      shift
      ;;
    --ack-stale)
      [ "${_lane_allow_ack_stale:-0}" = "1" ] \
        || _lane_args_die "--ack-stale is a Lane 3 option (staleness is only checked by the gate launcher)"
      _lane_args_need_value --ack-stale "$(($# - 1))"
      lane_ack_stale="$2"
      [ -n "$lane_ack_stale" ] || _lane_args_die "--ack-stale requires a non-empty reason"
      shift 2
      ;;
    --ack-stale=*)
      [ "${_lane_allow_ack_stale:-0}" = "1" ] \
        || _lane_args_die "--ack-stale is a Lane 3 option (staleness is only checked by the gate launcher)"
      lane_ack_stale="${1#--ack-stale=}"
      [ -n "$lane_ack_stale" ] || _lane_args_die "--ack-stale requires a non-empty reason"
      shift
      ;;
    *)
      lane_passthrough+=("$1")
      shift
      ;;
  esac
done

# An empty value is a typo, not a request for the default -- both forms are
# rejected at the branches above. `--ack-stale ""` in particular follows this
# codebase's own established precedent for an escape hatch requiring a real
# human justification: HRSE2/scripts/l1_post.py:810's `--ack-overlap`, and its
# test_empty_ack_overlap_reason_is_rejected_at_the_cli. An escape hatch that
# accepts an empty justification is not an escape hatch, it is a flag.
unset -f _lane_args_need_value
