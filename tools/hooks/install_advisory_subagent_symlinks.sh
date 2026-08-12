#!/usr/bin/env bash
# Install the 3 advisory subagents (pitch-inspection, product-strategy,
# sticky-wicket) as USER-LEVEL symlinks — harmonic-forge#237.
#
# Project-level subagent frontmatter hooks require workspace-trust
# acceptance for the folder containing the agent file; the untrusted case
# fails OPEN (subagent still runs, hooks silently skipped). `userSettings`-
# sourced subagent hooks (i.e. agent files under ~/.claude/agents/) are
# unconditionally permitted regardless of a given project's trust state
# (verified live against the installed Claude Code build, see this issue's
# handoff) — making the gh-write hook trust-exempt by construction only
# when installed this way. Existing project-level symlinks (e.g.
# HRSE2/.claude/agents/*.md) are left in place for discovery; enforcement
# no longer depends on them.
#
# Idempotent — safe to re-run. Absolute-path symlinks, matching the
# existing convention already in use for ai-review-queue-synthesis
# (harmonic-forge#768).

set -euo pipefail

# Deliberately hardcoded to the stable main checkout, NOT computed relative
# to this script's own location — this script may be run from a disposable
# impl worktree, but the symlink target must survive that worktree's
# cleanup. Matches the existing ai-review-queue-synthesis convention
# exactly (harmonic-forge#768: `~/.claude/agents/*.md ->
# /home/mmangus/harmonic-forge/agents/*.md`, not a worktree path).
FORGE_ROOT="$HOME/harmonic-forge"
TARGET_DIR="$HOME/.claude/agents"

mkdir -p "$TARGET_DIR"

for agent in pitch-inspection product-strategy sticky-wicket; do
    src="$FORGE_ROOT/agents/$agent.md"
    dest="$TARGET_DIR/$agent.md"
    if [ ! -f "$src" ]; then
        echo "ERROR: $src does not exist — refusing to link a missing source." >&2
        exit 1
    fi
    if [ -e "$dest" ] && [ ! -L "$dest" ]; then
        echo "ERROR: $dest exists and is not a symlink — refusing to overwrite. Remove it manually if intended." >&2
        exit 1
    fi
    ln -sf "$src" "$dest"
    echo "linked: $dest -> $src"
done
