# shellcheck shell=bash
# lane_identity <N> -- a lane's name and pane title from its repo (harmonic-forge#1014).
#
# Sourced by lane1/lane2/lane3 after argument parsing and every refusal, so a
# `--help` or a refused launch never retitles the pane. Sets LANE_LABEL
# (`vh:lane2`, or the bare `lane2` for an unregistered checkout) from $PWD via
# tools/onboard/manifest_labels.py. LANE_LABEL is a plain shell variable and is
# NEVER exported: a nested launch (run_canary.py, a test harness) must derive its
# own identity rather than inherit this one. _cli_launch.sh reads it to append
# the agent's name flag, and prints LANE_LABEL_NOTE (why a label is bare).
#
# Under tmux ($TMUX_PANE set) it turns the pane's allow-set-title off and sets
# the title to the label, so the agent's own title escapes cannot overwrite it.
# Both tmux calls are `|| true`: a title must never block a launch.
lane_identity() {
  local lane="lane$1" resolver out="" reason=""
  # An exported LANE_LABEL inherited from a parent lane's shell keeps its
  # export attribute through a plain assignment; unset drops it first.
  unset LANE_LABEL LANE_LABEL_NOTE
  resolver="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../onboard/manifest_labels.py"
  LANE_LABEL="$lane"
  if [ -f "$resolver" ]; then
    # No -I: the resolver imports its sibling `manifest` modules. One 2>&1
    # stream, label on the last line, reason before it -- no temp file, so a
    # broken TMPDIR cannot turn a title into a refused launch.
    out="$(python3 "$resolver" label "$PWD" "$lane" 2>&1)" || out=""
    LANE_LABEL="${out##*$'\n'}"
    [ "$LANE_LABEL" = "$out" ] || reason="${out%$'\n'*}"
    case "$LANE_LABEL" in ""|*[!A-Za-z0-9_.:-]*) reason="${reason:-$out}"; LANE_LABEL="$lane" ;; esac
  else
    reason="$resolver not found"
  fi
  # Printed by _cli_launch.sh step 4d, after its own refusals, so a refusal
  # still leads stderr. Plain variable, never exported, like LANE_LABEL.
  LANE_LABEL_NOTE=""
  if [ -n "$reason" ]; then
    LANE_LABEL_NOTE="$lane: session label '$LANE_LABEL' -- ${reason%%$'\n'*}"
  fi
  if [ -n "${TMUX_PANE:-}" ]; then
    tmux set-option -p -t "$TMUX_PANE" allow-set-title off >/dev/null 2>&1 || true
    tmux select-pane -t "$TMUX_PANE" -T "$LANE_LABEL" >/dev/null 2>&1 || true
  fi
}
