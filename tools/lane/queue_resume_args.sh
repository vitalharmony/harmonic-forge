#!/usr/bin/env bash
# Emit the Codex exec flags from the one lane registry; never duplicate them.
set -euo pipefail
lane="${1:?lane required}"
case "$lane" in 1|2|3) ;; *) exit 2 ;; esac
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/_agent_registry.sh"
sandbox="$(registry_lookup AGENT_LANE_SANDBOX "codex:$lane")"
add_dirs="$(registry_lookup AGENT_LANE_ADD_DIR "codex:$lane")"
session_flags="$(registry_lookup AGENT_SESSION_FLAGS codex)"
[ -z "$sandbox" ] || printf '%s\n' "--sandbox" "$sandbox"
for dir in $add_dirs; do printf '%s\n' "--add-dir" "$HOME/$dir"; done
for flag in $session_flags; do printf '%s\n' "$flag"; done
