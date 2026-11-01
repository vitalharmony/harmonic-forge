#!/usr/bin/env bash
# Install and enable the harmonic-forge#826 telemetry timers (and #1159's
# detector report, #1181's orphan reaper, and #1188's LLM-usage harvest) as user units.
# Idempotent: re-running copies the current unit files and re-enables them.
set -euo pipefail
# Stand down the extractor (harmonic-forge#907; the operator authorizes it):
#   systemctl --user disable --now forge-thread-extract.timer
# A failed run (keyring locked, machine off for days) leaves the watermark in
# place, so the next successful run re-covers the whole gap; extraction dedupes.
here="$(cd "$(dirname "$0")" && pwd)"
dest="$HOME/.config/systemd/user"
mkdir -p "$dest"
for unit in forge-transcript-backup forge-ci-history forge-thread-extract forge-detector-report forge-llm-usage; do
  install -m 644 "$here/systemd/$unit.service" "$here/systemd/$unit.timer" "$dest/"
done
# harmonic-forge#1181: the orphan reaper's units live beside it in tools/lane/.
install -m 644 "$here/../lane/systemd/forge-orphan-reap.service" \
  "$here/../lane/systemd/forge-orphan-reap.timer" "$dest/"
systemctl --user daemon-reload
systemctl --user enable --now forge-transcript-backup.timer forge-ci-history.timer forge-thread-extract.timer forge-detector-report.timer forge-orphan-reap.timer forge-llm-usage.timer
systemctl --user list-timers --no-pager | grep -E "forge-(transcript-backup|ci-history|thread-extract|detector-report|orphan-reap|llm-usage)"
