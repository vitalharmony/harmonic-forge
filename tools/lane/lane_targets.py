"""Pure session-target selection for tmux_lanes_claude.sh's `lane_sessions`
heredoc (hrse#2081).

The heredoc embedding this decision directly was untestable (the sibling
belt-lock reaper already made the same observation about itself and got
`lanes_reap.py` out of it — see `reap_stale_belt_locks`'s own comment).
This module extracts the actual decision — given a listing of Claude
sessions, which pids belong to which tmux panes, which session ids were
previously tracked, and which mode is running — into an importable, pure
function with no subprocess/tmux/filesystem calls of its own, so
`tools/lane/test_lane_targets.py` can exercise it directly against fixture
data.
"""
from __future__ import annotations

import os
import re
import subprocess

GIT_TIMEOUT_SECONDS = 5

STOP = "stop"
ADVISOR = "advisor"

# hrse#2081 AC2's own text: "processes under $advisor_session's panes, OR a
# background session named Advisor or Lane Advisor". This is deliberately
# NOT `lane_names` (which also has "Lane 1"/"Lane 2"/"Lane 3") -- advisor
# mode must never be able to match a lane session by name, only by these two
# literal, advisor-specific names.
ADVISOR_NAMES = {"Advisor", "Lane Advisor"}


def advisor_names() -> set[str]:
    """`ADVISOR_NAMES` plus every `<label>:advisor` (harmonic-forge#1014).

    Lazy: nothing reads projects.toml at import time, so this module's tests
    never touch the real manifest. Any failure (a missing tools/onboard, an
    unreadable or colliding manifest) falls back to the legacy literal set.
    """
    import sys
    onboard = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "onboard")
    try:
        if onboard not in sys.path:
            sys.path.insert(0, onboard)
        from manifest_labels import advisor_label_names
        return set(ADVISOR_NAMES) | advisor_label_names()
    except Exception:  # noqa: BLE001 -- teardown must never fail on a label
        return set(ADVISOR_NAMES)


def _git_common_dir(path: str) -> str | None:
    """The absolute git common dir of `path`, or None on any failure (a stale
    worktree, a non-repo, a timeout). The ONE subprocess in this module, isolated
    so tests can patch it (hrse#2258)."""
    try:
        done = subprocess.run(
            ["git", "-C", path, "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return None
    out = done.stdout.strip()
    return os.path.realpath(out) if done.returncode == 0 and out else None


def _git_toplevel(path: str) -> str | None:
    """The git toplevel containing `path`, or None (not a repository, a stale path, a
    timeout). Isolated, like `_git_common_dir`, so tests can patch it."""
    try:
        done = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return None
    out = done.stdout.strip()
    return os.path.realpath(out) if done.returncode == 0 and out else None


def workspace_dirs_from_paths(paths) -> set[str]:
    """The workspace directories implied by where its tmux panes are (hrse#2258).

    Teardown runs while the panes are still alive, so their current paths are an
    authoritative, always-available scope: no file has to be written at launch and kept in
    step. Each path resolves to its git toplevel (a path outside any repository, such as a
    pane back at a shell in `$HOME`, is ignored: a non-repo directory as a scope root would
    match every session under it), then, as the lane launchers do, a trailing `-laneN` is
    stripped and the main checkout plus `-lane2`/`-lane3` are recorded. An empty result means
    "scope unknown": `select_targets` then matches nothing by name."""
    dirs: set[str] = set()
    for path in paths:
        top = _git_toplevel(path)
        if not top:
            continue
        base = re.sub(r"-lane\d+$", "", top)
        dirs |= {base, base + "-lane2", base + "-lane3"}
    return dirs


def _under(path: str, root: str) -> bool:
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def in_workspace(entry: dict, workspace_dirs: set[str] | None, cache: dict | None = None) -> bool:
    """Does this `agents --json` entry belong to the workspace whose directories are
    `workspace_dirs` (hrse#2258)? False when no dirs are known or the entry has no `cwd`:
    the name fallback must never reach across workspaces on a guess. True when the `cwd`
    is a recorded dir or under one; otherwise true when its repository (git common dir)
    is one of the recorded dirs' repositories, which covers a per-issue worktree. A git
    failure counts as "not in the workspace". The recorded dirs' common dirs are computed
    once per call, through `cache`."""
    if not workspace_dirs:
        return False
    cwd = entry.get("cwd")
    if not cwd:
        return False
    if any(_under(cwd, d) for d in workspace_dirs):
        return True
    common = _git_common_dir(cwd)
    if common is None:
        return False
    cache = cache if cache is not None else {}
    if "commons" not in cache:
        cache["commons"] = {c for c in map(_git_common_dir, workspace_dirs) if c}
    return common in cache["commons"]


def select_targets(
    mode: str,
    listing: list[dict],
    panes: set[int],
    tracked: set[str],
    lane_names: set[str],
    me: set[int],
    workspace_dirs: set[str] | None = None,
    advisor_name_set: set[str] | None = None,
) -> list[dict]:
    """The `agents --json` entries `lane_sessions()` should stop.

    `mode="stop"` (also used for `guard`) is today's full-teardown
    behavior: a session counts as a target if its session id was tracked,
    its pid is under a workspace pane, or it is a background session whose
    name falls in `lane_names`.

    `mode="advisor"` (hrse#2081 AC2) is narrower in a different way, not
    stricter: it never consults `tracked` or the full `lane_names` (closing
    the advisor must never reach for the tracked-session state file, which
    belongs to `stop`'s own teardown, or match a *lane* session by name).
    But it MUST still catch the advisor's own Claude session when that
    session is daemon-hosted rather than pane-hosted -- Claude Code hosts
    sessions in its own background daemon (this script's own usage text),
    and a daemon-hosted `agents --json` entry carries no usable pane pid at
    all, only a `kind == "background"` + `name` it registered under. Pane
    membership alone can never match that shape, so the fix keeps a name
    check, scoped to exactly `ADVISOR_NAMES` (never the wider `lane_names`)
    so it can catch the advisor without ever being able to catch a lane.

    hrse#2258: both name fallbacks additionally require `in_workspace(entry,
    workspace_dirs)`. Every workspace's lanes carry the names `Lane 1`..`Lane 3` and
    `Advisor`, so a bare name match let closing one workspace's window stop every
    other workspace's daemon-hosted sessions. With no `workspace_dirs` (the scope could not be derived: no
    repository under the workspace's panes) the name fallback matches nothing: tracked ids and
    pane pids only.
    """
    cache: dict = {}
    names = ADVISOR_NAMES if advisor_name_set is None else advisor_name_set
    if mode == ADVISOR:
        return [
            a for a in listing
            if a.get("sessionId") is not None
            and a.get("pid") not in me
            and (
                a.get("pid") in panes
                or (a.get("kind") == "background" and a.get("name") in names
                    and in_workspace(a, workspace_dirs, cache))
            )
        ]
    return [
        a for a in listing
        if a.get("sessionId") is not None
        and a.get("pid") not in me
        and (
            a.get("sessionId") in tracked
            or a.get("pid") in panes
            # Name fallback only for daemon-hosted sessions: a live
            # interactive "Lane N" may belong to another workspace.
            or (a.get("kind") == "background" and a.get("name") in lane_names
                and in_workspace(a, workspace_dirs, cache))
        )
    ]
