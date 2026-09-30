#!/usr/bin/env bash
# Off-machine, encrypted backup of Claude Code transcripts (harmonic-forge#826).
#
# `~/.claude/projects/` holds every session and subagent transcript -- the raw
# record F637's telemetry backfills from -- as a single copy on a single disk,
# and Claude Code deletes it after `cleanupPeriodDays`. This keeps a restic
# repository of it inside an Insync-synced Google Drive folder, so the copy
# leaves the machine and Google only ever holds ciphertext.
#
# Nothing machine-specific is committed. The repository location and the
# password file come from a local config (never a repo, never a log):
#
#   ~/.config/harmonic-forge/transcript-backup.env
#     RESTIC_REPOSITORY=<a directory inside the Insync-synced Drive tree>
#     RESTIC_PASSWORD_FILE=<a mode-600 file OUTSIDE the synced tree>
#     SOURCE_DIR=~/.claude/projects          # optional
#
# Snapshots are never forgotten: the point is that nothing is lost, and
# restic deduplicates, so an unchanged transcript costs nothing per run.
# `restic check --read-data-subset` verifies a slice of stored data each run.
set -euo pipefail

CONF="${FORGE_TRANSCRIPT_BACKUP_ENV:-$HOME/.config/harmonic-forge/transcript-backup.env}"
if [[ ! -r "$CONF" ]]; then
  echo "transcript-backup: no config at $CONF (see this script's header)" >&2
  exit 2
fi
# shellcheck disable=SC1090
source "$CONF"
: "${RESTIC_REPOSITORY:?transcript-backup: RESTIC_REPOSITORY unset in $CONF}"
: "${RESTIC_PASSWORD_FILE:?transcript-backup: RESTIC_PASSWORD_FILE unset in $CONF}"
SOURCE_DIR="${SOURCE_DIR:-$HOME/.claude/projects}"
SOURCE_DIR="${SOURCE_DIR/#\~/$HOME}"
export RESTIC_REPOSITORY RESTIC_PASSWORD_FILE

perm=$(stat -c '%a' "$RESTIC_PASSWORD_FILE")
if [[ "$perm" != "600" && "$perm" != "400" ]]; then
  echo "transcript-backup: $RESTIC_PASSWORD_FILE is mode $perm; must be 600 or 400" >&2
  exit 2
fi

if ! restic cat config >/dev/null 2>&1; then
  mkdir -p "$RESTIC_REPOSITORY"
  restic init
fi

restic backup --tag claude-transcripts --one-file-system --no-scan "$SOURCE_DIR"

# The copy only protects anything once it has left the disk. Wait for Insync
# to report the whole repository uploaded -- the same `insync status -f`
# probe HRSE2's scripts/graph_backup.py uses (it always exits 0, so only its
# stdout counts) -- and fail loudly rather than report a local-only backup.
if command -v insync >/dev/null 2>&1; then
  deadline=$(( $(date +%s) + ${SYNC_TIMEOUT_S:-3600} ))
  state="$(insync status -f "$RESTIC_REPOSITORY" 2>/dev/null | head -1)"
  while [[ "$state" != "SYNCED" && $(date +%s) -lt $deadline ]]; do
    sleep "${SYNC_POLL_S:-15}"
    state="$(insync status -f "$RESTIC_REPOSITORY" 2>/dev/null | head -1)"
  done
  echo "transcript-backup: upload state $state"
  if [[ "$state" != "SYNCED" ]]; then
    echo "transcript-backup: repository not fully uploaded within ${SYNC_TIMEOUT_S:-3600}s" >&2
    exit 3
  fi
fi

restic check --read-data-subset=2%
