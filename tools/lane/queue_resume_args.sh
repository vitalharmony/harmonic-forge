#!/usr/bin/env bash
# Emit the Codex exec flags from the one lane registry; never duplicate them.
set -euo pipefail
lane="${1:?lane required}"
case "$lane" in 1|2|3) ;; *) exit 2 ;; esac
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/_agent_registry.sh"
registry_assert_integrity
# harmonic-forge#840: queued resumes keep a sandbox (no approval prompt in exec).
sandbox="$(registry_lookup AGENT_QUEUE_SANDBOX codex)"
add_dirs="$(registry_lookup AGENT_LANE_ADD_DIR "codex:$lane")"
session_flags="$(registry_lookup AGENT_SESSION_FLAGS codex)"
[ -z "$sandbox" ] || printf '%s\0' "--sandbox" "$sandbox"
for dir in $add_dirs; do printf '%s\0' "--add-dir" "$HOME/$dir"; done
for flag in $session_flags; do printf '%s\0' "$flag"; done
