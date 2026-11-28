#!/usr/bin/env bash
# Emit the Codex exec flags from the one lane registry; never duplicate them.
set -euo pipefail
lane="${1:?lane required}"
case "$lane" in 1|2|3) ;; *) exit 2 ;; esac
source "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/_agent_registry.sh"
registry_assert_integrity
# harmonic-forge#840: queued resumes keep a sandbox (no approval prompt in exec).
sandbox="$(registry_lookup AGENT_QUEUE_SANDBOX codex)"
# harmonic-forge#1529: the same expansion the launcher uses; unset projects_root refuses.
mapfile -d '' -t add_dirs < <(rc=0; registry_add_dirs "codex:$lane" || rc=$?; printf '%s\0' "$rc")
[ "${add_dirs[-1]}" = 0 ] || exit 1
unset 'add_dirs[-1]'
session_flags="$(registry_lookup AGENT_SESSION_FLAGS codex)"
[ -z "$sandbox" ] || printf '%s\0' "--sandbox" "$sandbox"
for dir in "${add_dirs[@]}"; do printf '%s\0' "--add-dir" "$dir"; done
for flag in $session_flags; do printf '%s\0' "$flag"; done
