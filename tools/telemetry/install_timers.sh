#!/usr/bin/env bash
# Install and enable the harmonic-forge#826 telemetry timers as user units.
# Idempotent: re-running copies the current unit files and re-enables them.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
dest="$HOME/.config/systemd/user"
mkdir -p "$dest"
for unit in forge-transcript-backup forge-ci-history; do
  install -m 644 "$here/systemd/$unit.service" "$here/systemd/$unit.timer" "$dest/"
done
systemctl --user daemon-reload
systemctl --user enable --now forge-transcript-backup.timer forge-ci-history.timer
systemctl --user list-timers --no-pager | grep -E "forge-(transcript-backup|ci-history)"
