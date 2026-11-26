#!/usr/bin/env bash
# The forge root and the platform-rules sync (harmonic-forge#1526, #651).
#
# Sourced by tools/lane/_cli_launch.sh only, immediately before the lane's
# settings are built, at every agent. Split out of _cli_launch.sh so that file
# stays within the line cap. Sets _lane_view_set, _lane_forge_root,
# _lane_forge_root_source and _lane_forge_root_written (`instance.launch_view`,
# the one view the onboarding report shares), which _cli_launch.sh passes to
# `lane_hookset.py build` and then unsets.
#
## The forge root (harmonic-forge#1526)
#
# HARMONIC_FORGE_ROOT, then instance.toml forge_root, then this launcher's own
# checkout (`instance.resolve_forge_root`). A bad value, or a root with no
# tools/hooks/, refuses the launch naming the value and its source. The
# view arrives as four NUL-ended fields, so a tab or newline in any of them
# (the instance.toml path in the source) cannot shift the others (post-verdict
# survivor 2); `root`'s exit status follows as a fifth, since a process
# substitution's status is otherwise lost.
mapfile -d '' -t _lane_root_out < <(python3 -I -E "$_lane_dir/lane_hookset.py" root --launcher-checkout "$_lane_dir/../.."; printf '%s\0' "$?")
{ [ "${#_lane_root_out[@]}" -eq 5 ] && [ "${_lane_root_out[4]}" = 0 ]; } \
  || _lane_launch_die "the forge root could not be resolved (see the reason above) -- refusing to launch (harmonic-forge#1526). Nothing was launched."
_lane_view_set="${_lane_root_out[0]}" _lane_forge_root="${_lane_root_out[1]}"
_lane_forge_root_source="${_lane_root_out[2]}" _lane_forge_root_written="${_lane_root_out[3]}"
# Every field is required: `launch_view` never prints an empty one, so an empty
# field under exit 0 is a broken producer, and an empty root would run the pull
# and the build from the current directory (post-verdict check 2, survivor 2).
_lane_root_empty=""
for _lane_root_field in "set:$_lane_view_set" "root:$_lane_forge_root" \
    "source:$_lane_forge_root_source" "as-written:$_lane_forge_root_written"; do
  [ -n "${_lane_root_field#*:}" ] || _lane_root_empty="$_lane_root_empty ${_lane_root_field%%:*}"
done
if [ -n "$_lane_root_empty" ]; then
  _lane_root_named="the forge root (its value as written is empty too)"
  [ -z "$_lane_forge_root_written" ] \
    || _lane_root_named="forge root '$_lane_forge_root_written' (from ${_lane_forge_root_source:-an empty source})"
  _lane_launch_die "$_lane_root_named: the root producer exited 0 with empty field(s):$_lane_root_empty -- refusing to launch (harmonic-forge#1526). Nothing was launched."
fi
unset _lane_root_empty _lane_root_field
# The root notice: one stderr line for any root not taken from the launcher
# checkout, before the pull so it precedes any pull output. It changes no argv.
# "launcher checkout" is instance.LAUNCHER_CHECKOUT, compared as a literal.
if [ "$_lane_forge_root_source" != "launcher checkout" ]; then
  echo "${_lane_name:-lane launcher}: forge root $_lane_forge_root (from $_lane_forge_root_source) -- its tools/hooks and sync_rules.py run for this session; the hook set stays $_lane_dir's (harmonic-forge#1526)" >&2
fi

## Platform-rules sync -- immediately before the final exec (harmonic-forge#651)
#
# A fast-forward-only pull of the forge root itself. The 2026-09-14 incident
# was a Lane 1 session running belt-mode tooling from a checkout 3 commits
# behind origin/main -- both the stale SKILL.md prose AND the stale script
# that still accepted a retired flag were served from the same stale
# checkout, so the launcher is the one place that can close that gap for
# every subsequent hook/skill/tool invocation in the session. Best-effort by
# design: this must never block session startup -- a warning and continue on
# any failure (missing sync_rules.py, no network, merge conflict a
# fast-forward-only pull refuses, timeout).
if ! timeout 30 python3 "$_lane_forge_root/sync_rules.py" --pull >&2; then
  echo "${_lane_name:-lane launcher}: sync_rules.py --pull failed or timed out -- continuing with the current $_lane_forge_root checkout" >&2
fi
unset _lane_root_out
